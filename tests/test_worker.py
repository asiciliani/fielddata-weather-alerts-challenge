import asyncio
import logging
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import worker as worker_module
from app.config import Settings
from app.enums import NotificationStatus
from app.models import Notification
from app.services.evaluation import try_evaluation_lock
from app.worker import Worker, heartbeat_is_fresh, run_periodic
from tests.conftest import TEST_DATABASE_URL
from tests.factories import add_member, make_alert, make_field, make_forecast, make_user, make_zone
from tests.fakes import FakeNotifier, channel_down


@pytest.fixture
def settings(test_settings: Settings, tmp_path: Path) -> Settings:
    return test_settings.model_copy(
        update={
            "worker_heartbeat_dir": tmp_path,
            "evaluation_interval_seconds": 0.05,
            "dispatch_interval_seconds": 0.05,
            "dispatch_batch_size": 2,
            "notifier_timeout_seconds": 2,
        }
    )


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def worker(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    notifier: FakeNotifier,
) -> Worker:
    return Worker(settings, session_factory, notifier)


def today() -> date:
    return datetime.now(UTC).astimezone(Settings(_env_file=None).tz).date()


async def alerts_with_dates(session: AsyncSession, count: int, dates: int = 1) -> None:
    """``count`` frost alerts, each on its own one-zone field, firing on ``dates`` dates."""
    for i in range(count):
        user = await make_user(session, f"U{i}")
        zone = await make_zone(session)
        field = await make_field(session, f"Campo {i}", zones=[(zone, "1")])
        await add_member(session, user, field)
        await make_alert(session, user, field, "frost", 70)
        for offset in range(1, dates + 1):
            await make_forecast(session, zone, "frost", today() + timedelta(days=offset), 90)
    await session.commit()


async def statuses(session: AsyncSession) -> list[NotificationStatus]:
    result = await session.execute(select(Notification.status).order_by(Notification.id))
    return list(result.scalars().all())


# --- periodic runner ---------------------------------------------------------------------------


async def test_run_periodic_repeats_and_stops_promptly() -> None:
    stop = asyncio.Event()
    runs = 0

    async def job() -> None:
        nonlocal runs
        runs += 1
        if runs == 3:
            stop.set()

    await asyncio.wait_for(run_periodic("t", 0.01, job, stop), 2)
    assert runs == 3


async def test_run_periodic_stops_while_waiting() -> None:
    stop = asyncio.Event()

    async def job() -> None:
        asyncio.get_running_loop().call_later(0.05, stop.set)

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(run_periodic("t", 60, job, stop), 2)
    assert asyncio.get_running_loop().time() - started < 1


async def test_failing_job_is_logged_and_the_next_tick_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    runs = 0

    async def job() -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("boom")
        stop.set()

    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(run_periodic("evaluation", 0.01, job, stop), 2)
    assert runs == 2
    [record] = [r for r in caplog.records if r.message == "job_failed"]
    assert record.levelno == logging.ERROR and record.exc_info is not None
    assert record.job == "evaluation"  # type: ignore[attr-defined]


async def test_slow_job_never_overlaps_itself() -> None:
    stop = asyncio.Event()
    running = 0
    peak = 0
    runs = 0

    async def job() -> None:
        nonlocal running, peak, runs
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)  # longer than the interval
        running -= 1
        runs += 1
        if runs == 3:
            stop.set()

    await asyncio.wait_for(run_periodic("t", 0.01, job, stop), 2)
    assert peak == 1


# --- heartbeats --------------------------------------------------------------------------------


async def test_evaluation_heartbeat_after_success_not_after_failure(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = worker.evaluation_heartbeat.path

    async def broken(*_: Any, **__: Any) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "evaluate_alerts", broken)
    with pytest.raises(RuntimeError):
        await worker.run_evaluation()
    assert not path.exists()
    monkeypatch.undo()
    await worker.run_evaluation()
    assert heartbeat_is_fresh(path, 5)


async def test_dispatch_heartbeat_even_when_nothing_to_send(worker: Worker) -> None:
    await worker.run_dispatch()
    assert heartbeat_is_fresh(worker.dispatch_heartbeat.path, 5)


async def test_dispatch_heartbeat_stays_fresh_during_a_long_run(
    worker: Worker, notifier: FakeNotifier, session: AsyncSession
) -> None:
    await alerts_with_dates(session, 6)
    await worker.run_evaluation()
    beats: list[float] = []
    real_beat = worker.dispatch_heartbeat.beat

    async def recording_beat() -> None:
        await real_beat()
        beats.append(asyncio.get_running_loop().time())

    worker.dispatch_heartbeat.beat = recording_beat  # type: ignore[method-assign]
    notifier.fail_with = channel_down()
    await worker.run_dispatch()
    assert len(beats) == 4  # three full batches of 2, then an empty one


def test_heartbeat_freshness(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.heartbeat"
    assert not heartbeat_is_fresh(path, 45)  # missing
    path.write_text("not a time")
    assert not heartbeat_is_fresh(path, 45)
    path.write_text((datetime.now(UTC) - timedelta(seconds=60)).isoformat())
    assert not heartbeat_is_fresh(path, 45)
    path.write_text(datetime.now(UTC).isoformat())
    assert heartbeat_is_fresh(path, 45)


def test_healthcheck_reads_only_the_dispatch_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: Settings(_env_file=None, worker_heartbeat_dir=tmp_path),
    )
    (tmp_path / "evaluation.heartbeat").write_text(datetime.now(UTC).isoformat())
    assert worker_module.main(["--healthcheck"]) == 1
    (tmp_path / "evaluation.heartbeat").write_text("stale")
    (tmp_path / "dispatch.heartbeat").write_text(datetime.now(UTC).isoformat())
    assert worker_module.main(["--healthcheck"]) == 0


# --- evaluation job ----------------------------------------------------------------------------


async def test_run_evaluation_commits_and_logs(
    worker: Worker, session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await alerts_with_dates(session, 1, dates=2)
    with caplog.at_level(logging.INFO):
        await worker.run_evaluation()
    assert await statuses(session) == [NotificationStatus.PENDING] * 2
    [done] = [r for r in caplog.records if r.message == "evaluation_completed"]
    assert done.notifications_created == 2  # type: ignore[attr-defined]


async def test_evaluation_is_skipped_while_another_replica_holds_the_lock(
    worker: Worker,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await alerts_with_dates(session, 1)
    async with session_factory() as other, other.begin():
        assert await try_evaluation_lock(other)
        with caplog.at_level(logging.INFO):
            await worker.run_evaluation()
            assert await worker.run_once() == 0  # a skip alone is not a failure
    assert any(r.message == "evaluation_skipped" for r in caplog.records)
    assert await statuses(session) == []
    assert heartbeat_is_fresh(worker.evaluation_heartbeat.path, 5)


async def test_two_evaluations_at_once_one_runs_one_skips(
    worker: Worker,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await alerts_with_dates(session, 3, dates=3)
    other = Worker(settings, session_factory, FakeNotifier())
    with caplog.at_level(logging.INFO):
        await asyncio.gather(worker.run_evaluation(), other.run_evaluation())
    messages = [r.message for r in caplog.records]
    assert messages.count("evaluation_completed") + messages.count("evaluation_skipped") == 2
    assert len(await statuses(session)) == 9


async def test_evaluation_retries_once_after_a_foreign_key_violation(
    worker: Worker, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0
    real = worker_module.evaluate_alerts  # type: ignore[attr-defined]

    class Orig(Exception):
        sqlstate = "23503"

    async def flaky(*args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise IntegrityError("INSERT", {}, Orig())
        return await real(*args, **kwargs)

    monkeypatch.setattr(worker_module, "evaluate_alerts", flaky)
    with caplog.at_level(logging.INFO):
        await worker.run_evaluation()
    assert calls == 2
    assert [r.levelno for r in caplog.records if r.message == "evaluation_retried"] == [
        logging.INFO
    ]


async def test_evaluation_fails_after_a_second_foreign_key_violation(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Orig(Exception):
        sqlstate = "23503"

    async def always(*_: Any, **__: Any) -> int:
        raise IntegrityError("INSERT", {}, Orig())

    monkeypatch.setattr(worker_module, "evaluate_alerts", always)
    with pytest.raises(IntegrityError):
        await worker.run_evaluation()


async def test_evaluation_sets_its_own_statement_timeout(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    async def spy(session: AsyncSession, **_: Any) -> int:
        from sqlalchemy import text

        seen.append((await session.execute(text("SHOW statement_timeout"))).scalar_one())
        return 0

    monkeypatch.setattr(worker_module, "evaluate_alerts", spy)
    await worker.run_evaluation()
    assert seen == ["4min"]


# --- dispatch job and shutdown -----------------------------------------------------------------


async def test_run_dispatch_drains_several_batches(
    worker: Worker,
    notifier: FakeNotifier,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await alerts_with_dates(session, 5)
    await worker.run_evaluation()
    with caplog.at_level(logging.INFO):
        await worker.run_dispatch()
    assert len(notifier.sent) == 5
    assert await statuses(session) == [NotificationStatus.SENT] * 5
    [done] = [r for r in caplog.records if r.message == "dispatch_completed"]
    assert done.sent == 5 and done.claimed == 5  # type: ignore[attr-defined]


async def test_run_dispatch_stops_between_batches(
    worker: Worker, notifier: FakeNotifier, session: AsyncSession
) -> None:
    await alerts_with_dates(session, 5)
    await worker.run_evaluation()
    worker.stop.set()
    await worker.run_dispatch()
    assert len(notifier.sent) == 2  # one batch, then it noticed the stop


async def test_first_stop_drains_the_batch_in_flight(
    worker: Worker, notifier: FakeNotifier, session: AsyncSession
) -> None:
    await alerts_with_dates(session, 4)
    await worker.run_evaluation()
    notifier.gate = asyncio.Event()
    task = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(notifier.entered.wait(), 5)
    worker.request_stop()
    notifier.gate.set()
    await asyncio.wait_for(task, 5)
    assert len(notifier.sent) == 2  # the batch in flight, and no new one
    assert sorted(await statuses(session)) == sorted(
        [NotificationStatus.SENT] * 2 + [NotificationStatus.PENDING] * 2
    )


async def test_second_stop_cancels_and_leaves_rows_pending(
    worker: Worker, notifier: FakeNotifier, session: AsyncSession
) -> None:
    await alerts_with_dates(session, 2)
    await worker.run_evaluation()
    notifier.gate = asyncio.Event()  # never opened
    task = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(notifier.entered.wait(), 5)
    worker.request_stop()
    worker.request_stop()
    await asyncio.wait_for(task, 5)  # returns although the notifier never answered
    assert notifier.sent == []
    rows = (await session.execute(select(Notification))).scalars().all()
    assert [(r.status, r.attempts) for r in rows] == [(NotificationStatus.PENDING, 0)] * 2


async def test_stop_cancels_an_evaluation_in_progress(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    async def slow(*_: Any, **__: Any) -> int:
        entered.set()
        await asyncio.sleep(60)
        return 0

    monkeypatch.setattr(worker_module, "evaluate_alerts", slow)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(entered.wait(), 5)
    worker.request_stop()
    await asyncio.wait_for(task, 5)  # did not wait the 60 seconds


# --- single-cycle mode -------------------------------------------------------------------------


async def test_once_returns_non_zero_when_a_job_fails(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "dispatch_batch", broken)
    assert await worker.run_once() == 1


async def test_once_after_a_stop_does_nothing(worker: Worker) -> None:
    worker.request_stop()
    assert await worker.run_once() == 1


async def test_run_once_through_the_entry_point(settings: Settings, session: AsyncSession) -> None:
    await alerts_with_dates(session, 1, dates=2)
    notifier = FakeNotifier()
    assert await worker_module.run(settings, once=True, notifier=notifier) == 0
    assert len(notifier.sent) == 1


def test_module_entry_point_once_exits_zero(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": TEST_DATABASE_URL,
        "WORKER_HEARTBEAT_DIR": str(tmp_path),
        "LOG_FORMAT": "json",
    }
    done = subprocess.run(
        [sys.executable, "-m", "app.worker", "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert '"message": "worker_stopped"' in done.stdout
    assert (tmp_path / "dispatch.heartbeat").exists()


# --- end to end --------------------------------------------------------------------------------


async def test_end_to_end_from_a_new_alert_to_a_sent_notification(
    client: AsyncClient, worker: Worker, notifier: FakeNotifier, session: AsyncSession
) -> None:
    user = await make_user(session, "Ana")
    a, b = await make_zone(session), await make_zone(session)
    field = await make_field(session, "Los Álamos", zones=[(a, "0.6"), (b, "0.4")])
    await add_member(session, user, field)
    first, second = today() + timedelta(days=2), today() + timedelta(days=3)
    await make_forecast(session, a, "frost", first, 40)
    await make_forecast(session, b, "frost", first, 80)
    await make_forecast(session, a, "frost", second, 75)
    await make_forecast(session, b, "frost", second, 90)
    await session.commit()
    headers = {"X-User-Id": str(user.id)}

    created = await client.post(
        "/api/v1/alerts",
        json={"field_id": field.id, "event_type": "frost", "threshold": 70},
        headers=headers,
    )
    assert created.status_code == 201
    assert await worker.run_once() == 0

    [message] = notifier.sent
    assert message.text == (
        f'⚠️ Alerta de helada en "Los Álamos" (umbral 70%): '
        f"{first:%d/%m} 80% (40% del campo), {second:%d/%m} 90%."
    )
    listed = await client.get("/api/v1/notifications", headers=headers)
    items = listed.json()["items"]
    assert [(n["forecast_date"], n["status"], n["affected_area"]) for n in items] == [
        (second.isoformat(), "sent", "1.0000"),
        (first.isoformat(), "sent", "0.4000"),
    ]
    assert all(n["sent_at"] for n in items)
    assert Decimal(items[1]["affected_area"]) == Decimal("0.4")
