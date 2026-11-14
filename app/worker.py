"""The worker process: evaluation and dispatch, each on its own interval.

``python -m app.worker`` runs both loops until SIGTERM or SIGINT. The first signal drains: the
dispatch batch in flight is finished and saved, no new one is taken, and an evaluation in
progress is cancelled (it is safe to repeat). A second signal cancels everything; the batch in
flight rolls back and its notifications stay pending.

``python -m app.worker --once`` runs evaluation once, then dispatch until a batch is not full,
and exits non-zero if either failed. ``python -m app.worker --healthcheck`` exits non-zero when
the dispatch heartbeat is stale (the container's health check).
"""

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import build_engine, build_session_factory, database_now
from app.logging import configure_logging
from app.notifiers import LogNotifier, Notifier
from app.services.dispatch import DispatchResult, DispatchSettings, dispatch_batch
from app.services.evaluation import check_data, evaluate_alerts, try_evaluation_lock

logger = logging.getLogger(__name__)

EVALUATION_STATEMENT_TIMEOUT = "240s"  # below the 5-minute interval, above the 30 s default
HEARTBEAT_STALE_INTERVALS = 3
_FOREIGN_KEY_VIOLATION = "23503"


async def run_periodic(
    name: str, interval: float, job: Callable[[], Awaitable[None]], stop: asyncio.Event
) -> None:
    """Run ``job``, then wait for the rest of the interval or until ``stop`` is set.

    A failing run is logged with its traceback and the loop keeps going. Runs never overlap:
    the next one starts only after the previous one returned.
    """
    while not stop.is_set():
        started = time.monotonic()
        try:
            await job()
        except Exception:
            logger.exception("job_failed", extra={"job": name})
        elapsed = time.monotonic() - started
        logger.debug("job_finished", extra={"job": name, "duration_ms": round(elapsed * 1000)})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, interval - elapsed))


class Heartbeat:
    """A file holding the time of a job's last successful progress."""

    def __init__(self, directory: Path, job: str) -> None:
        self.path = directory / f"{job}.heartbeat"

    async def beat(self) -> None:
        # A file write is blocking I/O, so it runs in a thread (concurrency rule 4).
        stamp = datetime.now(UTC).isoformat()
        await asyncio.to_thread(self.path.write_text, stamp)


def heartbeat_is_fresh(path: Path, max_age_seconds: float) -> bool:
    """For the health check process (synchronous on purpose: it runs outside the worker)."""
    try:
        stamp = datetime.fromisoformat(path.read_text().strip())
    except (OSError, ValueError):
        return False
    return (datetime.now(UTC) - stamp).total_seconds() <= max_age_seconds


class Worker:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: Notifier,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.notifier = notifier
        self.stop = asyncio.Event()
        self.evaluation_heartbeat = Heartbeat(settings.worker_heartbeat_dir, "evaluation")
        self.dispatch_heartbeat = Heartbeat(settings.worker_heartbeat_dir, "dispatch")
        self.dispatch_settings = DispatchSettings(
            batch_size=settings.dispatch_batch_size,
            send_timeout=settings.notifier_timeout_seconds,
            horizon_days=settings.evaluation_horizon_days,
            min_affected_area=settings.alert_min_affected_area,
            backoff_base=settings.notification_backoff_base_seconds,
            backoff_max=settings.notification_backoff_max_seconds,
        )
        self._evaluation_task: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._signals = 0

    # --- evaluation ----------------------------------------------------------------------------

    async def run_evaluation(self) -> None:
        """One evaluation run in one transaction; skipped if another replica holds the lock.

        An alert deleted while the statement runs fails its foreign key; the run is retried
        once (INFO), and a second failure is an error.
        """
        started = time.monotonic()
        for attempt in (1, 2):
            try:
                created = await self._evaluate()
            except IntegrityError as exc:
                if attempt == 1 and getattr(exc.orig, "sqlstate", None) == _FOREIGN_KEY_VIOLATION:
                    logger.info("evaluation_retried", extra={"reason": "foreign_key_violation"})
                    continue
                raise
            break
        if created is not None:
            logger.info(
                "evaluation_completed",
                extra={
                    "notifications_created": created,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            await self._check_data()
        await self.evaluation_heartbeat.beat()

    async def _evaluate(self) -> int | None:
        """Returns how many notifications were created, or None when the run was skipped."""
        async with self.session_factory() as session, session.begin():
            await session.execute(
                sa.text(f"SET LOCAL statement_timeout = '{EVALUATION_STATEMENT_TIMEOUT}'")
            )
            if not await try_evaluation_lock(session):
                logger.info("evaluation_skipped", extra={"reason": "another replica evaluates"})
                return None
            today = (await database_now(session)).astimezone(self.settings.tz).date()
            return await evaluate_alerts(
                session,
                today=today,
                horizon_days=self.settings.evaluation_horizon_days,
                min_affected_area=self.settings.alert_min_affected_area,
            )

    async def _check_data(self) -> None:
        async with self.session_factory() as session, session.begin():
            now = await database_now(session)
            await check_data(
                session,
                now=now,
                today=now.astimezone(self.settings.tz).date(),
                horizon_days=self.settings.evaluation_horizon_days,
                max_age_hours=self.settings.forecast_max_age_hours,
            )

    # --- dispatch ------------------------------------------------------------------------------

    async def run_dispatch(self) -> None:
        """Batches, one transaction each, until a batch is not full or a stop is requested.

        The heartbeat is written after every batch, so a long outage keeps it fresh.
        """
        total = DispatchResult()
        while True:
            async with self.session_factory() as session, session.begin():
                now = await database_now(session)
                result = await dispatch_batch(
                    session,
                    self.notifier,
                    now=now,
                    today=now.astimezone(self.settings.tz).date(),
                    settings=self.dispatch_settings,
                )
            await self.dispatch_heartbeat.beat()
            for name in ("claimed", "sent", "retried", "expired", "discarded"):
                setattr(total, name, getattr(total, name) + getattr(result, name))
            if result.claimed < self.dispatch_settings.batch_size or self.stop.is_set():
                break
        if total.processed:
            logger.info("dispatch_completed", extra=vars(total))

    # --- process -------------------------------------------------------------------------------

    def request_stop(self) -> None:
        """First call drains; a second one cancels whatever is still running."""
        self._signals += 1
        if self._signals == 1:
            logger.info("shutdown_requested")
            self.stop.set()
            if self._evaluation_task is not None:
                self._evaluation_task.cancel()
            return
        logger.warning("shutdown_forced")
        for task in self._tasks:
            task.cancel()

    async def run_forever(self) -> None:
        async with asyncio.TaskGroup() as group:
            self._evaluation_task = group.create_task(
                run_periodic(
                    "evaluation",
                    self.settings.evaluation_interval_seconds,
                    self.run_evaluation,
                    self.stop,
                )
            )
            dispatch_task = group.create_task(
                run_periodic(
                    "dispatch",
                    self.settings.dispatch_interval_seconds,
                    self.run_dispatch,
                    self.stop,
                )
            )
            self._tasks = [self._evaluation_task, dispatch_task]

    async def run_once(self) -> int:
        """Evaluation once (a skip is fine), then dispatch. Returns 1 if either job failed or a
        stop request cut the run short, else 0."""
        failed = False
        for name, job in (("evaluation", self.run_evaluation), ("dispatch", self.run_dispatch)):
            if self.stop.is_set():
                return 1
            task: asyncio.Task[None] = asyncio.create_task(job())
            self._tasks = [task]
            self._evaluation_task = task if name == "evaluation" else None
            try:
                await task
            except asyncio.CancelledError:
                if not self.stop.is_set():
                    raise  # cancelled from outside, not by a stop request
                return 1
            except Exception:
                logger.exception("job_failed", extra={"job": name})
                failed = True
        self._evaluation_task = None
        return 1 if failed else 0


async def run(settings: Settings, *, once: bool, notifier: Notifier | None = None) -> int:
    engine = build_engine(settings, worker=True)
    worker = Worker(settings, build_session_factory(engine), notifier or LogNotifier())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)
    try:
        if once:
            return await worker.run_once()
        logger.info("worker_started")
        await worker.run_forever()
        return 0
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        await engine.dispose()
        logger.info("worker_stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weather alerts worker.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run each job once and exit")
    mode.add_argument(
        "--healthcheck", action="store_true", help="exit 1 if the dispatch heartbeat is stale"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.healthcheck:
        max_age = HEARTBEAT_STALE_INTERVALS * settings.dispatch_interval_seconds
        path = Heartbeat(settings.worker_heartbeat_dir, "dispatch").path
        return 0 if heartbeat_is_fresh(path, max_age) else 1
    configure_logging(settings)
    return asyncio.run(run(settings, once=args.once))


if __name__ == "__main__":
    sys.exit(main())
