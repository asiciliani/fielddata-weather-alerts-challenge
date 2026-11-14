import asyncio
import dataclasses
import logging
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import FieldRole, NotificationStatus
from app.models import Alert, Field, Notification, User, WeatherForecast, Zone
from app.notifiers import LogNotifier, NotifierError, OutboundMessage
from app.services import dispatch as dispatch_module
from app.services.dispatch import (
    DispatchResult,
    DispatchSettings,
    backoff_delay,
    claim_alerts_statement,
    dispatch_batch,
    render_message,
)
from app.services.evaluation import evaluate_alerts
from tests.factories import (
    add_member,
    make_alert,
    make_field,
    make_forecast,
    make_notification,
    make_user,
    make_zone,
)
from tests.fakes import FakeNotifier, channel_down

NOW = datetime(2026, 11, 13, 15, tzinfo=UTC)
TODAY = date(2026, 11, 13)
SETTINGS = DispatchSettings(
    batch_size=20,
    send_timeout=1,
    horizon_days=7,
    min_affected_area=Decimal("0.10"),
    backoff_base=60,
    backoff_max=1800,
)


def day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


async def dispatch(
    session: AsyncSession,
    notifier: FakeNotifier,
    *,
    now: datetime = NOW,
    today: date = TODAY,
    **overrides: object,
) -> DispatchResult:
    settings = dataclasses.replace(SETTINGS, **overrides)  # type: ignore[arg-type]
    return await dispatch_batch(
        session, notifier, now=now, today=today, settings=settings, rng=random.Random(1)
    )


async def rows(session: AsyncSession) -> list[Notification]:
    result = await session.execute(
        select(Notification)
        .order_by(Notification.alert_id, Notification.forecast_date)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


class Story:
    """One user, one field in one zone, one frost alert at 70, forecasts and pending rows."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def build(
        self, probabilities: dict[int, int], *, name: str = "La Esperanza", user: User | None = None
    ) -> "Story":
        s = self.session
        self.user = user or await make_user(s, "Ana", phone_number=f"+54 {id(self)}")
        self.zone: Zone = await make_zone(s)
        self.field: Field = await make_field(s, name, zones=[(self.zone, "1")])
        await add_member(s, self.user, self.field)
        self.alert: Alert = await make_alert(s, self.user, self.field, "frost", 70)
        self.forecasts: dict[int, WeatherForecast] = {}
        for offset, probability in probabilities.items():
            self.forecasts[offset] = await make_forecast(
                s, self.zone, "frost", day(offset), probability
            )
        return self

    async def pending(
        self, offset: int, *, at: datetime = NOW, attempts: int = 0, probability: int | None = None
    ) -> Notification:
        forecast = self.forecasts.get(offset)
        return await make_notification(
            self.session,
            self.alert,
            day(offset),
            next_attempt_at=at,
            attempts=attempts,
            forecast=forecast,
            probability=probability
            if probability is not None
            else (forecast.probability if forecast else 80),
        )


# --- pure functions ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "dates", "expected"),
    [
        (
            "helada",
            [(day(2), 85, Decimal(1))],
            '⚠️ Alerta de helada en "La Esperanza" (umbral 70%): 15/11 85%.',
        ),
        (
            "lluvia",
            [(day(3), 72, Decimal(1)), (day(2), 85, Decimal(1))],
            '⚠️ Alerta de lluvia en "La Esperanza" (umbral 70%): 15/11 85%, 16/11 72%.',
        ),
        (
            "viento fuerte",
            [(day(2), 85, Decimal("0.6")), (day(3), 75, Decimal("0.996"))],
            '⚠️ Alerta de viento fuerte en "La Esperanza" (umbral 70%): '
            "15/11 85% (60% del campo), 16/11 75%.",
        ),
        (
            "calor extremo",
            [(day(2), 85, Decimal("0.6")), (day(3), 75, Decimal(1)), (day(5), 71, Decimal("0.4"))],
            '⚠️ Alerta de calor extremo en "La Esperanza" (umbral 70%): '
            "15/11 85% (60% del campo), 16/11 75%, 18/11 71% (40% del campo).",
        ),
    ],
)
def test_render_message(event: str, dates: list[tuple[date, int, Decimal]], expected: str) -> None:
    assert render_message("La Esperanza", event, 70, dates) == expected


def test_render_message_rounds_the_share() -> None:
    text_ = render_message("F", "helada", 70, [(day(1), 80, Decimal("0.125"))])
    assert "(13% del campo)" in text_


def test_backoff_bounds_and_cap() -> None:
    rng = random.Random(3)
    for attempts, ceiling in [(1, 60), (2, 120), (3, 240), (6, 1800), (50, 1800)]:
        values = [backoff_delay(attempts, base=60, cap=1800, rng=rng) for _ in range(200)]
        assert all(0 <= v <= ceiling for v in values)
        assert max(values) > ceiling * 0.8  # full jitter spreads over the whole range


def test_backoff_does_not_overflow_on_huge_attempt_counts() -> None:
    assert 0 <= backoff_delay(2000, base=60, cap=1800, rng=random.Random(0)) <= 1800


def test_claim_locks_alerts_without_blocking_evaluation() -> None:
    statement = claim_alerts_statement(now=NOW, batch_size=5)
    sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert sql.endswith("FOR NO KEY UPDATE OF alerts SKIP LOCKED")
    assert "ORDER BY due.tries >" in sql


async def test_log_notifier_never_logs_the_phone_number(caplog: pytest.LogCaptureFixture) -> None:
    message = OutboundMessage(
        alert_id=1, user_id=2, notification_ids=(3, 4), to="+5491155556666", text="hola"
    )
    with caplog.at_level(logging.INFO):
        await LogNotifier().send(message)
    [record] = [r for r in caplog.records if r.message == "message_delivered"]
    assert record.notification_ids == [3, 4] and record.text == "hola"  # type: ignore[attr-defined]
    assert "5556666" not in str(record.__dict__)


# --- delivery ----------------------------------------------------------------------------------


async def test_pending_becomes_sent_to_the_alerts_user(session: AsyncSession) -> None:
    story = await Story(session).build({2: 85})
    await story.pending(2)
    notifier = FakeNotifier()
    result = await dispatch(session, notifier)
    assert (result.claimed, result.sent) == (1, 1)
    [message] = notifier.sent
    assert message.to == story.user.phone_number and message.user_id == story.user.id
    assert message.text == '⚠️ Alerta de helada en "La Esperanza" (umbral 70%): 15/11 85%.'
    [row] = await rows(session)
    assert row.status is NotificationStatus.SENT and row.sent_at == NOW


async def test_two_dates_of_one_alert_go_in_one_message(session: AsyncSession) -> None:
    story = await Story(session).build({2: 85, 3: 72})
    await story.pending(3)
    await story.pending(2)
    notifier = FakeNotifier()
    await dispatch(session, notifier)
    [message] = notifier.sent
    assert message.text.endswith("15/11 85%, 16/11 72%.")
    assert all(r.status is NotificationStatus.SENT for r in await rows(session))


async def test_two_alerts_give_two_messages(session: AsyncSession) -> None:
    first = await Story(session).build({2: 85})
    second = await Story(session).build({2: 90}, name="Los Álamos")
    await first.pending(2)
    await second.pending(2)
    notifier = FakeNotifier()
    result = await dispatch(session, notifier)
    assert result.claimed == 2 and len(notifier.sent) == 2


async def test_cuj1_from_evaluation_to_one_message(session: AsyncSession) -> None:
    await Story(session).build({2: 85, 3: 72, 4: 40})
    assert (
        await evaluate_alerts(
            session, today=TODAY, horizon_days=7, min_affected_area=Decimal("0.1")
        )
        == 2
    )
    notifier = FakeNotifier()
    await dispatch(session, notifier, now=datetime.now(UTC))
    assert [m.text for m in notifier.sent] == [
        '⚠️ Alerta de helada en "La Esperanza" (umbral 70%): 15/11 85%, 16/11 72%.'
    ]


# --- what is claimed ---------------------------------------------------------------------------


async def test_future_retry_is_not_claimed(session: AsyncSession) -> None:
    story = await Story(session).build({2: 85})
    await story.pending(2, at=NOW + timedelta(seconds=1))
    notifier = FakeNotifier()
    assert (await dispatch(session, notifier)).claimed == 0
    assert notifier.attempted == []


async def test_alert_is_due_only_when_all_its_pending_rows_are(session: AsyncSession) -> None:
    story = await Story(session).build({2: 85, 3: 80})
    await story.pending(2)
    await story.pending(3, at=NOW + timedelta(minutes=1))
    assert (await dispatch(session, FakeNotifier())).claimed == 0


async def test_sent_and_failed_rows_are_never_claimed(session: AsyncSession) -> None:
    story = await Story(session).build({2: 85, 3: 85})
    for offset, status in [(2, NotificationStatus.SENT), (3, NotificationStatus.FAILED)]:
        await make_notification(
            session, story.alert, day(offset), next_attempt_at=NOW, status=status
        )
    assert (await dispatch(session, FakeNotifier())).claimed == 0


async def test_batch_size_counts_alerts(session: AsyncSession) -> None:
    for i in range(3):
        story = await Story(session).build({2: 85, 3: 85}, name=f"Campo {i}")
        await story.pending(2)
        await story.pending(3)
    notifier = FakeNotifier()
    result = await dispatch(session, notifier, batch_size=2)
    assert result.claimed == 2 and result.sent == 4 and len(notifier.sent) == 2


async def test_first_attempts_are_claimed_before_older_retries(session: AsyncSession) -> None:
    retrying = await Story(session).build({2: 85}, name="Reintento")
    await retrying.pending(2, at=NOW - timedelta(hours=2), attempts=4)
    fresh = await Story(session).build({2: 85}, name="Nuevo")
    await fresh.pending(2, at=NOW - timedelta(minutes=1))
    notifier = FakeNotifier()
    await dispatch(session, notifier, batch_size=1)
    assert [m.alert_id for m in notifier.sent] == [fresh.alert.id]


async def test_skips_an_alert_that_is_no_longer_due_on_a_fresh_snapshot(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    story = await Story(session).build({2: 85, 3: 85})
    await story.pending(2)
    later = await story.pending(3)
    await session.commit()
    real_claim = dispatch_module._claim_alerts

    async def claim_then_other_dispatcher_records_a_failure(*args: Any, **kwargs: Any) -> list[int]:
        ids = await real_claim(*args, **kwargs)
        async with session_factory() as other, other.begin():
            await other.execute(
                update(Notification)
                .where(Notification.id == later.id)
                .values(next_attempt_at=NOW + timedelta(minutes=5))
            )
        return ids

    monkeypatch.setattr(
        dispatch_module, "_claim_alerts", claim_then_other_dispatcher_records_a_failure
    )
    async with session_factory() as s, s.begin():
        notifier = FakeNotifier()
        result = await dispatch(s, notifier)
    assert result.claimed == 1 and notifier.attempted == []


# --- expiry and re-check -----------------------------------------------------------------------


async def test_past_date_is_failed_and_reported_for_the_caller_to_log(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    story = await Story(session).build({})
    first = await story.pending(-2, probability=90)
    second = await story.pending(-1, probability=85)
    notifier = FakeNotifier()
    with caplog.at_level(logging.ERROR):
        result = await dispatch(session, notifier)
    assert result.expired == 2 and notifier.attempted == []
    assert {r.status for r in await rows(session)} == {NotificationStatus.FAILED}
    assert result.expired_groups == [(story.alert.id, story.user.id, [first.id, second.id])]
    # Logged by the worker after the batch commits, never before.
    assert not [r for r in caplog.records if r.message == "notification_expired"]


async def test_today_can_still_be_sent(session: AsyncSession) -> None:
    story = await Story(session).build({0: 85})
    await story.pending(0)
    notifier = FakeNotifier()
    assert (await dispatch(session, notifier)).sent == 1


@pytest.mark.parametrize("change", [{"is_active": False}, {"threshold": 90}])
async def test_past_date_of_an_alert_the_user_changed_is_deleted_quietly(
    session: AsyncSession, caplog: pytest.LogCaptureFixture, change: dict[str, object]
) -> None:
    story = await Story(session).build({})
    await story.pending(-1, probability=85)
    await session.execute(update(Alert).where(Alert.id == story.alert.id).values(**change))
    with caplog.at_level(logging.ERROR):
        result = await dispatch(session, FakeNotifier())
    assert result.discarded == 1 and result.expired == 0
    assert await rows(session) == []
    assert not [r for r in caplog.records if r.message == "notification_expired"]


@pytest.mark.parametrize(
    "change",
    ["deactivate", "raise_threshold", "forecast_dropped", "forecast_purged"],
)
async def test_dates_that_no_longer_qualify_are_deleted_not_sent(
    session: AsyncSession, change: str, caplog: pytest.LogCaptureFixture
) -> None:
    story = await Story(session).build({2: 85})
    await story.pending(2)
    alert_q = update(Alert).where(Alert.id == story.alert.id)
    forecast_id = story.forecasts[2].id
    match change:
        case "deactivate":
            await session.execute(alert_q.values(is_active=False))
        case "raise_threshold":
            await session.execute(alert_q.values(threshold=90))
        case "forecast_dropped":
            await session.execute(
                update(WeatherForecast)
                .where(WeatherForecast.id == forecast_id)
                .values(probability=50)
            )
        case "forecast_purged":
            await session.execute(
                text("DELETE FROM weather_forecasts WHERE id = :id"), {"id": forecast_id}
            )
    notifier = FakeNotifier()
    with caplog.at_level(logging.INFO):
        result = await dispatch(session, notifier)
    assert result.discarded == 1 and notifier.attempted == []
    assert await rows(session) == []
    assert any(r.message == "stale_notifications_discarded" for r in caplog.records)


async def test_a_date_that_still_qualifies_is_sent_with_refreshed_values(
    session: AsyncSession,
) -> None:
    story = await Story(session).build({2: 85})
    row = await story.pending(2)
    other_zone = await make_zone(session)
    await session.execute(
        text("DELETE FROM field_zones WHERE field_id = :f"), {"f": story.field.id}
    )
    await session.execute(
        text(
            "INSERT INTO field_zones (field_id, zone_id, coverage) VALUES "
            "(:f, :a, 0.6), (:f, :b, 0.4)"
        ),
        {"f": story.field.id, "a": story.zone.id, "b": other_zone.id},
    )
    await session.execute(
        update(WeatherForecast)
        .where(WeatherForecast.id == story.forecasts[2].id)
        .values(probability=40)
    )
    new_forecast = await make_forecast(session, other_zone, "frost", day(2), 95)
    await session.execute(update(Alert).where(Alert.id == story.alert.id).values(threshold=75))
    notifier = FakeNotifier()
    await dispatch(session, notifier)
    [message] = notifier.sent
    assert message.text == (
        '⚠️ Alerta de helada en "La Esperanza" (umbral 75%): 15/11 95% (40% del campo).'
    )
    [sent] = await rows(session)
    assert sent.id == row.id
    assert (sent.threshold, sent.probability, sent.affected_area, sent.forecast_id) == (
        75,
        95,
        Decimal("0.4"),
        new_forecast.id,
    )


async def test_reactivated_alert_is_detected_again(session: AsyncSession) -> None:
    story = await Story(session).build({2: 85})
    await story.pending(2)
    alert_q = update(Alert).where(Alert.id == story.alert.id)
    await session.execute(alert_q.values(is_active=False))
    await dispatch(session, FakeNotifier())
    assert await rows(session) == []
    await session.execute(alert_q.values(is_active=True))
    assert (
        await evaluate_alerts(
            session, today=TODAY, horizon_days=7, min_affected_area=Decimal("0.1")
        )
        == 1
    )


# --- failures ----------------------------------------------------------------------------------


async def test_failure_schedules_one_retry_for_the_whole_group(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    story = await Story(session).build({2: 85, 3: 85})
    await story.pending(2, attempts=1)
    await story.pending(3)
    notifier = FakeNotifier()
    notifier.fail_with = channel_down()
    with caplog.at_level(logging.WARNING):
        result = await dispatch(session, notifier)
    assert result.retried == 2
    first, second = await rows(session)
    for row in (first, second):
        assert row.status is NotificationStatus.PENDING
        assert row.attempts == 2
        assert row.last_error == "unavailable: 503"
        assert row.next_attempt_at > NOW
    assert first.next_attempt_at == second.next_attempt_at
    [warning] = [r for r in caplog.records if r.message == "notification_send_failed"]
    assert warning.levelno == logging.WARNING
    assert warning.attempts == 2 and warning.error_code == "503"  # type: ignore[attr-defined]


async def test_many_failures_never_fail_a_future_date(session: AsyncSession) -> None:
    story = await Story(session).build({5: 85})
    await story.pending(5)
    notifier = FakeNotifier()
    notifier.fail_with = channel_down()
    now = NOW
    for _ in range(12):
        await dispatch(session, notifier, now=now)
        [row] = await rows(session)
        now = row.next_attempt_at
    assert row.status is NotificationStatus.PENDING and row.attempts == 12


async def test_timeout_counts_as_a_failure(session: AsyncSession) -> None:
    story = await Story(session).build({2: 85})
    await story.pending(2)
    notifier = FakeNotifier()
    notifier.hang = True
    result = await dispatch(session, notifier, send_timeout=0.05)
    assert result.retried == 1
    [row] = await rows(session)
    assert row.attempts == 1 and row.last_error == "timeout: send_timeout"


async def test_unexpected_error_in_one_group_does_not_affect_the_others(
    session: AsyncSession,
) -> None:
    good = await Story(session).build({2: 85}, name="Bien")
    bad = await Story(session).build({2: 85}, name="Mal")
    await good.pending(2)
    await bad.pending(2)
    notifier = FakeNotifier()
    notifier.fail_with = lambda m: (
        RuntimeError("+5491100000000 is invalid") if m.alert_id == bad.alert.id else None
    )
    result = await dispatch(session, notifier)
    assert (result.sent, result.retried) == (1, 1)
    by_alert = {r.alert_id: r for r in await rows(session)}
    assert by_alert[good.alert.id].status is NotificationStatus.SENT
    assert by_alert[bad.alert.id].last_error == "unexpected: RuntimeError"  # no raw text


async def test_last_error_is_truncated(session: AsyncSession) -> None:
    story = await Story(session).build({2: 85})
    await story.pending(2)
    notifier = FakeNotifier()
    notifier.fail_with = NotifierError("x" * 400, "y" * 400)
    await dispatch(session, notifier)
    [row] = await rows(session)
    assert row.last_error is not None and len(row.last_error) == 500


async def test_cancelled_send_is_reraised_and_counts_nothing(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    story = await Story(session).build({2: 85})
    await story.pending(2)
    await session.commit()
    notifier = FakeNotifier()
    notifier.fail_with = asyncio.CancelledError()
    async with session_factory() as other:
        with pytest.raises(asyncio.CancelledError):
            async with other.begin():
                await dispatch(other, notifier)
    [row] = await rows(session)
    assert (row.status, row.attempts, row.last_error) == (NotificationStatus.PENDING, 0, None)


# --- concurrency -------------------------------------------------------------------------------


async def test_a_date_detected_during_a_failing_send_gets_the_new_retry_time(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    story = await Story(session).build({2: 85, 3: 50, 4: 90})
    await story.pending(2, probability=80)  # the batch refreshes it to 85: an UPDATE it holds
    await story.pending(3, probability=75)  # no longer qualifies: a DELETE the batch holds
    await session.commit()
    notifier = FakeNotifier()
    notifier.gate = asyncio.Event()
    notifier.fail_with = channel_down()

    async def run_dispatch() -> DispatchResult:
        async with session_factory() as s, s.begin():
            return await dispatch(s, notifier)

    task = asyncio.create_task(run_dispatch())
    await asyncio.wait_for(notifier.entered.wait(), 5)
    async with session_factory() as s, s.begin():  # evaluation does not wait for the batch
        created = await asyncio.wait_for(
            evaluate_alerts(s, today=TODAY, horizon_days=7, min_affected_area=Decimal("0.1")), 5
        )
    assert created == 1
    notifier.gate.set()
    await asyncio.wait_for(task, 5)
    first, new = await rows(session)
    assert new.forecast_date == day(4) and new.status is NotificationStatus.PENDING
    assert new.attempts == 0  # it was never attempted
    assert new.next_attempt_at == first.next_attempt_at > NOW


async def test_a_date_detected_during_a_successful_send_waits_for_the_next_message(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    story = await Story(session).build({2: 85, 4: 90})
    await story.pending(2)
    await session.commit()
    notifier = FakeNotifier()
    notifier.gate = asyncio.Event()

    async def run_dispatch() -> DispatchResult:
        async with session_factory() as s, s.begin():
            return await dispatch(s, notifier)

    task = asyncio.create_task(run_dispatch())
    await asyncio.wait_for(notifier.entered.wait(), 5)
    async with session_factory() as s, s.begin():
        await evaluate_alerts(s, today=TODAY, horizon_days=7, min_affected_area=Decimal("0.1"))
    notifier.gate.set()
    await asyncio.wait_for(task, 5)
    first, new = await rows(session)
    assert first.status is NotificationStatus.SENT
    assert new.status is NotificationStatus.PENDING


async def test_concurrent_dispatchers_never_send_the_same_notification_twice(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    stories = [await Story(session).build({2: 85, 3: 80}, name=f"Campo {i}") for i in range(4)]
    for story in stories:
        await story.pending(2)
        await story.pending(3)
    await session.commit()
    blocked, free = FakeNotifier(), FakeNotifier()
    blocked.gate = asyncio.Event()

    async def run(notifier: FakeNotifier, batch_size: int) -> DispatchResult:
        async with session_factory() as s, s.begin():
            return await dispatch(s, notifier, batch_size=batch_size)

    first = asyncio.create_task(run(blocked, 2))
    await asyncio.wait_for(blocked.entered.wait(), 5)
    second = await asyncio.wait_for(run(free, 20), 5)  # skips the alerts the first one holds
    blocked.gate.set()
    await asyncio.wait_for(first, 5)
    assert second.claimed == 2
    ids = [i for n in (blocked, free) for m in n.sent for i in m.notification_ids]
    assert len(ids) == len(set(ids)) == 8
    assert {m.alert_id for m in blocked.sent}.isdisjoint({m.alert_id for m in free.sent})
    assert all(r.status is NotificationStatus.SENT for r in await rows(session))


async def test_two_members_get_separate_messages(session: AsyncSession) -> None:
    ana = await make_user(session, "Ana")
    beto = await make_user(session, "Beto")
    zone = await make_zone(session)
    field = await make_field(session, "La Compartida", zones=[(zone, "1")])
    await add_member(session, ana, field)
    await add_member(session, beto, field, FieldRole.AGRONOMIST)
    await make_forecast(session, zone, "heat", day(2), 90)
    for user in (ana, beto):
        alert = await make_alert(session, user, field, "heat", 60)
        await make_notification(session, alert, day(2), next_attempt_at=NOW, probability=90)
    notifier = FakeNotifier()
    await dispatch(session, notifier)
    assert sorted(m.to for m in notifier.sent) == sorted([ana.phone_number, beto.phone_number])
