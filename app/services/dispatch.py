"""Dispatch: deliver pending notifications, one grouped message per alert.

One call handles one batch inside the caller's transaction: claim alerts whose pending
notifications are all due, expire past dates, re-check the rest against the current alert and
forecasts, send one message per alert concurrently, and record each result. The transaction
stays open during the sends (bounded by the send timeout), so a crash rolls the batch back and
PostgreSQL releases its locks; delivery is at least once.
"""

import asyncio
import logging
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import NotificationStatus
from app.models import Alert, Notification
from app.notifiers import Notifier, NotifierError, OutboundMessage
from app.services.evaluation import firing_matches, window_end

logger = logging.getLogger(__name__)

LAST_ERROR_MAX_LENGTH = 500
_PENDING = NotificationStatus.PENDING.value


@dataclass(frozen=True)
class DispatchSettings:
    batch_size: int
    send_timeout: float
    horizon_days: int
    min_affected_area: Decimal
    backoff_base: float
    backoff_max: float


@dataclass
class DispatchResult:
    """Counts of one batch: ``claimed`` is in alerts, the rest in notifications."""

    claimed: int = 0
    sent: int = 0
    retried: int = 0
    expired: int = 0
    discarded: int = 0

    @property
    def processed(self) -> bool:
        return any((self.sent, self.retried, self.expired, self.discarded))


def backoff_delay(attempts: int, *, base: float, cap: float, rng: random.Random) -> float:
    """Full jitter: uniform in [0, min(cap, base * 2^(attempts-1))]; the exponent is capped
    because retries have no attempt limit."""
    exponent = min(max(attempts, 1) - 1, 30)
    return rng.uniform(0, min(cap, base * 2**exponent))


def _share(area: Decimal) -> int:
    return int((area * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def render_message(
    field_name: str, event_name: str, threshold: int, dates: list[tuple[date, int, Decimal]]
) -> str:
    """The Spanish WhatsApp text: dates ascending as dd/mm, with the share of the field only when
    it rounds below 100%."""
    parts = []
    for day, probability, area in sorted(dates):
        part = f"{day:%d/%m} {probability}%"
        if _share(area) < 100:
            part += f" ({_share(area)}% del campo)"
        parts.append(part)
    return f'⚠️ Alerta de {event_name} en "{field_name}" (umbral {threshold}%): {", ".join(parts)}.'


def claim_alerts_statement(*, now: datetime, batch_size: int) -> sa.Select[int]:
    """Lock up to ``batch_size`` alerts whose pending notifications are all due.

    First attempts come before retries, then the oldest; alerts another dispatcher holds are
    skipped. ``FOR NO KEY UPDATE`` does not block evaluation inserting new notifications.
    """
    due = (
        sa.select(
            Notification.alert_id,
            sa.func.min(Notification.next_attempt_at).label("oldest"),
            sa.func.max(Notification.attempts).label("tries"),
        )
        .where(Notification.status == _PENDING)
        .group_by(Notification.alert_id)
        .having(sa.func.max(Notification.next_attempt_at) <= now)
        .subquery("due")
    )
    return (
        sa.select(Alert.id)
        .join(due, due.c.alert_id == Alert.id)
        .order_by(due.c.tries > 0, due.c.oldest, Alert.id)
        .limit(batch_size)
        .with_for_update(key_share=True, skip_locked=True, of=Alert)
    )


async def _claim_alerts(session: AsyncSession, *, now: datetime, batch_size: int) -> list[int]:
    stmt = claim_alerts_statement(now=now, batch_size=batch_size)
    return list((await session.execute(stmt)).scalars().all())


async def _claim_notifications(
    session: AsyncSession, alert_ids: list[int], *, now: datetime
) -> list[Notification]:
    """All pending notifications of the locked alerts that are still due on a fresh snapshot
    (another dispatcher may have recorded a failure since the alerts were picked)."""
    still_due = (
        sa.select(Notification.alert_id)
        .where(Notification.status == _PENDING, Notification.alert_id.in_(alert_ids))
        .group_by(Notification.alert_id)
        .having(sa.func.max(Notification.next_attempt_at) <= now)
    )
    stmt = (
        sa.select(Notification)
        .where(Notification.status == _PENDING, Notification.alert_id.in_(still_due))
        .order_by(Notification.alert_id, Notification.forecast_date)
        .with_for_update(of=Notification)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _load_alerts(session: AsyncSession, alert_ids: set[int]) -> dict[int, Alert]:
    # Separate query: PostgreSQL rejects FOR UPDATE on the nullable side of an outer join.
    rows = await session.execute(
        sa.select(Alert)
        .where(Alert.id.in_(alert_ids))
        .options(selectinload(Alert.user), selectinload(Alert.field))
    )
    return {alert.id: alert for alert in rows.scalars().all()}


async def _expire(
    session: AsyncSession,
    rows: list[Notification],
    alerts: dict[int, Alert],
    result: DispatchResult,
) -> None:
    """Past dates can no longer be useful: failed if the user still wanted them, else deleted."""
    failed: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        alert = alerts[row.alert_id]
        if alert.is_active and alert.threshold <= row.probability:
            row.status = NotificationStatus.FAILED
            failed[row.alert_id].append(row.id)
        else:
            await session.delete(row)
            result.discarded += 1
    for alert_id, ids in failed.items():
        result.expired += len(ids)
        logger.error(
            "notification_expired",
            extra={
                "alert_id": alert_id,
                "user_id": alerts[alert_id].user_id,
                "notification_ids": ids,
            },
        )


async def _recheck(
    session: AsyncSession,
    rows: list[Notification],
    *,
    today: date,
    settings: DispatchSettings,
    result: DispatchResult,
) -> list[Notification]:
    """Keep the dates that still qualify, with refreshed values; delete the others."""
    if not rows:
        return []
    stmt = firing_matches(
        today=today,
        end_date=window_end(today, settings.horizon_days),
        min_affected_area=settings.min_affected_area,
        where=[Alert.id.in_({row.alert_id for row in rows})],
    )
    current = {(m.alert_id, m.forecast_date): m for m in (await session.execute(stmt)).all()}
    kept = []
    discarded = 0
    for row in rows:
        match = current.get((row.alert_id, row.forecast_date))
        if match is None:
            await session.delete(row)
            discarded += 1
            continue
        row.threshold = match.threshold
        row.probability = match.probability
        row.affected_area = match.affected_area
        row.forecast_id = match.forecast_id
        kept.append(row)
    if discarded:
        result.discarded += discarded
        logger.info("stale_notifications_discarded", extra={"count": discarded})
    return kept


async def _send(notifier: Notifier, message: OutboundMessage, seconds: float) -> None:
    async with asyncio.timeout(seconds):
        await notifier.send(message)


def _describe(error: BaseException) -> tuple[str, str]:
    if isinstance(error, NotifierError):
        return error.kind, error.code
    if isinstance(error, TimeoutError):
        return "timeout", "send_timeout"
    return "unexpected", type(error).__name__


async def dispatch_batch(
    session: AsyncSession,
    notifier: Notifier,
    *,
    now: datetime,
    today: date,
    settings: DispatchSettings,
    rng: random.Random | None = None,
) -> DispatchResult:
    """Handle one batch in the caller's transaction (the caller commits).

    ``now`` is timezone-aware (the database clock) and ``today`` is its date in the forecast
    timezone. A cancelled send is re-raised, so the caller's transaction rolls back and every
    claimed row stays as it was; it is never counted as a failed attempt.
    """
    rng = rng or random.Random()
    result = DispatchResult()
    alert_ids = await _claim_alerts(session, now=now, batch_size=settings.batch_size)
    result.claimed = len(alert_ids)
    if not alert_ids:
        return result
    rows = await _claim_notifications(session, alert_ids, now=now)
    if not rows:
        return result
    alerts = await _load_alerts(session, {row.alert_id for row in rows})

    await _expire(session, [r for r in rows if r.forecast_date < today], alerts, result)
    current = [r for r in rows if r.forecast_date >= today]
    kept = await _recheck(session, current, today=today, settings=settings, result=result)
    await session.flush()  # expiries, deletions and refreshed values, before anything is sent

    groups: dict[int, list[Notification]] = defaultdict(list)
    for row in kept:
        groups[row.alert_id].append(row)
    messages = []
    for alert_id, group in groups.items():
        alert = alerts[alert_id]
        messages.append(
            OutboundMessage(
                alert_id=alert_id,
                user_id=alert.user_id,
                notification_ids=tuple(row.id for row in group),
                to=alert.user.phone_number,
                text=render_message(
                    alert.field.name,
                    alert.event_type.name,
                    group[0].threshold,
                    [(r.forecast_date, r.probability, r.affected_area) for r in group],
                ),
            )
        )

    outcomes = await asyncio.gather(
        *(_send(notifier, m, settings.send_timeout) for m in messages), return_exceptions=True
    )
    for outcome in outcomes:
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome  # a second stop signal: roll back, leave every row pending

    for message, outcome in zip(messages, outcomes, strict=True):
        ids = list(message.notification_ids)
        if outcome is None:
            await session.execute(
                sa.update(Notification)
                .where(Notification.id.in_(ids))
                .values(status=NotificationStatus.SENT, sent_at=now, updated_at=sa.func.now())
                .execution_options(synchronize_session=False)
            )
            result.sent += len(ids)
            continue
        await _record_failure(
            session, message, outcome, groups[message.alert_id], now=now, settings=settings, rng=rng
        )
        result.retried += len(ids)
    return result


async def _record_failure(
    session: AsyncSession,
    message: OutboundMessage,
    error: BaseException,
    group: list[Notification],
    *,
    now: datetime,
    settings: DispatchSettings,
    rng: random.Random,
) -> None:
    """Count one attempt for the whole group and give the alert one new retry time.

    The retry time goes to every pending row of the alert, including one evaluation inserted
    while this batch was sending, so the alert's dates stay on one schedule.
    """
    attempts = max(row.attempts for row in group) + 1
    delay = backoff_delay(attempts, base=settings.backoff_base, cap=settings.backoff_max, rng=rng)
    next_attempt_at = now + timedelta(seconds=delay)
    kind, code = _describe(error)
    last_error = f"{kind}: {code}"[:LAST_ERROR_MAX_LENGTH]
    await session.execute(
        sa.update(Notification)
        .where(Notification.id.in_(message.notification_ids))
        .values(attempts=attempts, last_error=last_error, updated_at=sa.func.now())
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        sa.update(Notification)
        .where(Notification.alert_id == message.alert_id, Notification.status == _PENDING)
        .values(next_attempt_at=next_attempt_at, updated_at=sa.func.now())
        .execution_options(synchronize_session=False)
    )
    logger.warning(
        "notification_send_failed",
        extra={
            "alert_id": message.alert_id,
            "user_id": message.user_id,
            "notification_ids": list(message.notification_ids),
            "attempts": attempts,
            "error_kind": kind,
            "error_code": code,
            "retry_in_seconds": round(delay, 1),
        },
    )
