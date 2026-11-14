import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import FieldRole, NotificationStatus
from app.models import Alert, Field, Notification, User, WeatherForecast, Zone
from app.seed import seed
from app.services.evaluation import check_data, evaluate_alerts, try_evaluation_lock
from tests.factories import (
    add_member,
    make_alert,
    make_field,
    make_forecast,
    make_notification,
    make_user,
    make_zone,
)

TODAY = date(2026, 11, 13)
HORIZON = 7
MIN_AREA = Decimal("0.10")


def day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


async def evaluate(session: AsyncSession, min_area: Decimal = MIN_AREA) -> int:
    return await evaluate_alerts(
        session, today=TODAY, horizon_days=HORIZON, min_affected_area=min_area
    )


async def notifications(session: AsyncSession) -> list[Notification]:
    result = await session.execute(
        select(Notification).order_by(Notification.alert_id, Notification.forecast_date)
    )
    return list(result.scalars().all())


async def one_zone_field(session: AsyncSession) -> tuple[User, Field, Zone]:
    user = await make_user(session)
    zone = await make_zone(session)
    field = await make_field(session, zones=[(zone, "1")])
    await add_member(session, user, field)
    return user, field, zone


# --- one zone, coverage 1 ----------------------------------------------------------------------


async def test_above_threshold_creates_one_notification_with_its_snapshot(
    session: AsyncSession,
) -> None:
    user, field, zone = await one_zone_field(session)
    alert = await make_alert(session, user, field, "frost", 70)
    forecast = await make_forecast(session, zone, "frost", day(2), 85)
    assert await evaluate(session) == 1
    [n] = await notifications(session)
    assert (n.alert_id, n.forecast_id, n.forecast_date) == (alert.id, forecast.id, day(2))
    assert (n.threshold, n.probability, n.affected_area) == (70, 85, Decimal("1.0000"))
    assert n.status is NotificationStatus.PENDING and n.attempts == 0


@pytest.mark.parametrize(("probability", "created"), [(70, 1), (69, 0), (100, 1)])
async def test_threshold_is_inclusive(
    session: AsyncSession, probability: int, created: int
) -> None:
    user, field, zone = await one_zone_field(session)
    await make_alert(session, user, field, "frost", 70)
    await make_forecast(session, zone, "frost", day(1), probability)
    assert await evaluate(session) == created


async def test_inactive_alert_is_ignored(session: AsyncSession) -> None:
    user, field, zone = await one_zone_field(session)
    await make_alert(session, user, field, "frost", 50, is_active=False)
    await make_forecast(session, zone, "frost", day(1), 90)
    assert await evaluate(session) == 0


async def test_other_zone_or_event_type_does_not_match(session: AsyncSession) -> None:
    user, field, _ = await one_zone_field(session)
    elsewhere = await make_zone(session)
    await make_alert(session, user, field, "frost", 50)
    await make_forecast(session, elsewhere, "frost", day(1), 90)
    zone_of_field = (await session.execute(select(Zone).order_by(Zone.id))).scalars().first()
    assert zone_of_field is not None
    await make_forecast(session, zone_of_field, "rain", day(1), 90)
    assert await evaluate(session) == 0


@pytest.mark.parametrize(("offset", "created"), [(-1, 0), (0, 1), (HORIZON, 1), (HORIZON + 1, 0)])
async def test_window_is_today_to_today_plus_horizon(
    session: AsyncSession, offset: int, created: int
) -> None:
    user, field, zone = await one_zone_field(session)
    await make_alert(session, user, field, "frost", 50)
    await make_forecast(session, zone, "frost", day(offset), 90)
    assert await evaluate(session) == created


async def test_second_run_creates_nothing(session: AsyncSession) -> None:
    user, field, zone = await one_zone_field(session)
    await make_alert(session, user, field, "frost", 50)
    for offset in (1, 2, 3):
        await make_forecast(session, zone, "frost", day(offset), 90)
    assert await evaluate(session) == 3
    assert await evaluate(session) == 0
    assert len(await notifications(session)) == 3


async def test_forecast_rising_later_creates_only_the_new_date(session: AsyncSession) -> None:
    user, field, zone = await one_zone_field(session)
    await make_alert(session, user, field, "frost", 70)
    await make_forecast(session, zone, "frost", day(2), 85)
    later = await make_forecast(session, zone, "frost", day(5), 40)
    assert await evaluate(session) == 1
    await session.execute(
        update(WeatherForecast).where(WeatherForecast.id == later.id).values(probability=90)
    )
    assert await evaluate(session) == 1
    assert [n.forecast_date for n in await notifications(session)] == [day(2), day(5)]


async def test_lowering_the_threshold_picks_up_new_dates(session: AsyncSession) -> None:
    user, field, zone = await one_zone_field(session)
    alert = await make_alert(session, user, field, "frost", 80)
    await make_forecast(session, zone, "frost", day(1), 85)
    await make_forecast(session, zone, "frost", day(2), 75)
    assert await evaluate(session) == 1
    await session.execute(update(Alert).where(Alert.id == alert.id).values(threshold=70))
    assert await evaluate(session) == 1
    assert len(await notifications(session)) == 2


async def test_no_alerts_and_no_zones_create_nothing(session: AsyncSession) -> None:
    assert await evaluate(session) == 0
    user = await make_user(session)
    field = await make_field(session)  # Agrobot has not mapped it yet
    await add_member(session, user, field)
    await make_alert(session, user, field, "frost", 10)
    zone = await make_zone(session)
    await make_forecast(session, zone, "frost", day(1), 99)
    assert await evaluate(session) == 0


# --- several zones -----------------------------------------------------------------------------


async def two_zone_field(
    session: AsyncSession, coverages: tuple[str, str]
) -> tuple[Alert, Zone, Zone]:
    user = await make_user(session)
    a, b = await make_zone(session), await make_zone(session)
    field = await make_field(session, zones=[(a, coverages[0]), (b, coverages[1])])
    await add_member(session, user, field)
    return await make_alert(session, user, field, "frost", 70), a, b


async def test_partial_area_below_the_minimum_does_not_fire(session: AsyncSession) -> None:
    _, a, b = await two_zone_field(session, ("0.6", "0.4"))
    await make_forecast(session, a, "frost", day(1), 40)
    await make_forecast(session, b, "frost", day(1), 80)
    assert await evaluate(session, Decimal("0.5")) == 0


async def test_partial_area_above_the_minimum_fires_with_its_area(session: AsyncSession) -> None:
    _, a, b = await two_zone_field(session, ("0.6", "0.4"))
    await make_forecast(session, a, "frost", day(1), 40)
    forecast_b = await make_forecast(session, b, "frost", day(1), 80)
    assert await evaluate(session, Decimal("0.3")) == 1
    [n] = await notifications(session)
    assert (n.affected_area, n.probability, n.forecast_id) == (Decimal("0.4"), 80, forecast_b.id)


async def test_small_zones_add_up(session: AsyncSession) -> None:
    user = await make_user(session)
    zones = [await make_zone(session) for _ in range(3)]
    field = await make_field(
        session, zones=[(zones[0], "0.06"), (zones[1], "0.06"), (zones[2], "0.88")]
    )
    await add_member(session, user, field)
    await make_alert(session, user, field, "rain", 60)
    await make_forecast(session, zones[0], "rain", day(1), 70)
    await make_forecast(session, zones[1], "rain", day(1), 65)
    await make_forecast(session, zones[2], "rain", day(1), 10)
    assert await evaluate(session) == 1
    [n] = await notifications(session)
    assert n.affected_area == Decimal("0.12")


async def test_both_zones_give_one_notification_with_the_highest_probability(
    session: AsyncSession,
) -> None:
    _, a, b = await two_zone_field(session, ("0.6", "0.4"))
    await make_forecast(session, a, "frost", day(1), 72)
    higher = await make_forecast(session, b, "frost", day(1), 75)
    assert await evaluate(session) == 1
    [n] = await notifications(session)
    assert (n.affected_area, n.probability, n.forecast_id) == (Decimal(1), 75, higher.id)


async def test_notification_forecast_is_the_alerts_type_and_one_of_its_zones(
    session: AsyncSession,
) -> None:
    """Rule 6 of the conceptual model, enforced by the statement."""
    alert, a, b = await two_zone_field(session, ("0.5", "0.5"))
    other = await make_zone(session)
    for zone in (a, b, other):
        for code in ("frost", "rain"):
            await make_forecast(session, zone, code, day(1), 95)
    await evaluate(session)
    [n] = await notifications(session)
    forecast = await session.get(WeatherForecast, n.forecast_id)
    assert forecast is not None
    assert forecast.event_type_id == alert.event_type_id
    assert forecast.zone_id in (a.id, b.id)


# --- users, schedules and concurrency ----------------------------------------------------------


async def test_two_members_by_their_own_threshold(session: AsyncSession) -> None:
    ana, beto = await make_user(session, "Ana"), await make_user(session, "Beto")
    zone = await make_zone(session)
    field = await make_field(session, zones=[(zone, "1")])
    await add_member(session, ana, field)
    await add_member(session, beto, field, FieldRole.AGRONOMIST)
    low = await make_alert(session, ana, field, "frost", 60)
    await make_alert(session, beto, field, "frost", 80)
    await make_forecast(session, zone, "frost", day(1), 70)
    assert await evaluate(session) == 1
    assert [n.alert_id for n in await notifications(session)] == [low.id]


async def test_new_date_joins_its_alerts_pending_retry(session: AsyncSession) -> None:
    user, field, zone = await one_zone_field(session)
    alert = await make_alert(session, user, field, "frost", 70)
    retry_at = datetime(2030, 1, 1, tzinfo=UTC)
    await make_notification(session, alert, day(1), next_attempt_at=retry_at, attempts=3)
    await make_notification(
        session, alert, day(2), next_attempt_at=retry_at - timedelta(minutes=5), attempts=3
    )
    await make_forecast(session, zone, "frost", day(4), 90)
    assert await evaluate(session) == 1
    new = (await notifications(session))[-1]
    assert new.forecast_date == day(4)
    assert new.next_attempt_at == retry_at  # the latest of the alert's pending rows


async def test_sent_rows_do_not_hold_back_a_new_date(session: AsyncSession) -> None:
    user, field, zone = await one_zone_field(session)
    alert = await make_alert(session, user, field, "frost", 70)
    await make_notification(
        session,
        alert,
        day(1),
        next_attempt_at=datetime(2030, 1, 1, tzinfo=UTC),
        status=NotificationStatus.SENT,
    )
    await make_forecast(session, zone, "frost", day(1), 90)  # already sent: not again
    await make_forecast(session, zone, "frost", day(3), 90)
    before = datetime.now(UTC)
    assert await evaluate(session) == 1
    new = (await notifications(session))[-1]
    assert new.forecast_date == day(3)
    assert new.next_attempt_at < before + timedelta(minutes=1)  # now(), not the sent row's time


async def test_concurrent_runs_create_no_duplicates(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user, field, zone = await one_zone_field(session)
    await make_alert(session, user, field, "frost", 50)
    for offset in range(HORIZON + 1):
        await make_forecast(session, zone, "frost", day(offset), 90)
    await session.commit()

    async def run() -> int:
        async with session_factory() as s, s.begin():
            return await evaluate(s)

    results = await asyncio.wait_for(asyncio.gather(run(), run()), timeout=20)
    assert sum(results) == HORIZON + 1
    assert len(await notifications(session)) == HORIZON + 1


async def test_advisory_lock_lets_one_transaction_in(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as first, first.begin():
        assert await try_evaluation_lock(first)
        async with session_factory() as second, second.begin():
            assert not await try_evaluation_lock(second)
    async with session_factory() as third, third.begin():
        assert await try_evaluation_lock(third)  # released when the transaction ended


# --- data checks -------------------------------------------------------------------------------


async def _check(session: AsyncSession, now: datetime | None = None) -> None:
    await check_data(
        session,
        now=now or datetime.now(UTC),
        today=TODAY,
        horizon_days=HORIZON,
        max_age_hours=24,
    )


def _warnings(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message == message and r.levelno == logging.WARNING]


async def test_no_warnings_on_good_data(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    user, field, zone = await one_zone_field(session)
    await make_alert(session, user, field, "frost", 50)
    for offset in range(HORIZON + 1):
        await make_forecast(session, zone, "frost", day(offset), 10)
    with caplog.at_level(logging.WARNING):
        await _check(session)
    assert not _warnings(caplog, "alerts_without_zones")
    assert not _warnings(caplog, "forecasts_stale")


async def test_warns_about_alerts_without_zones_and_partial_coverage(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    user = await make_user(session)
    unmapped = await make_field(session, "Sin zonas")
    zone = await make_zone(session)
    partial = await make_field(session, "Parcial", zones=[(zone, "0.7")])
    await add_member(session, user, unmapped)
    await add_member(session, user, partial)
    await make_alert(session, user, unmapped, "frost", 50)
    await make_alert(session, user, unmapped, "rain", 50, is_active=False)
    with caplog.at_level(logging.WARNING):
        await _check(session)
    [record] = _warnings(caplog, "alerts_without_zones")
    assert record.alerts_without_zones == 1  # type: ignore[attr-defined]
    assert record.fields_with_partial_coverage == 1  # type: ignore[attr-defined]


async def test_warns_per_zone_when_a_date_is_missing(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    _, fresh, gappy = await two_zone_field(session, ("0.5", "0.5"))
    for offset in range(HORIZON + 1):
        await make_forecast(session, fresh, "frost", day(offset), 10)
        if offset != 3:
            await make_forecast(session, gappy, "frost", day(offset), 10)
    with caplog.at_level(logging.WARNING):
        await _check(session)
    [record] = _warnings(caplog, "forecasts_stale")
    assert record.stale_zones == 1  # type: ignore[attr-defined]
    assert record.zone_ids == [gappy.id]  # type: ignore[attr-defined]


async def test_warns_when_forecasts_are_old(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    user, field, zone = await one_zone_field(session)
    await make_alert(session, user, field, "frost", 50)
    for offset in range(HORIZON + 1):
        await make_forecast(session, zone, "frost", day(offset), 10)
    with caplog.at_level(logging.WARNING):
        await _check(session, now=datetime.now(UTC) + timedelta(hours=25))
    [record] = _warnings(caplog, "forecasts_stale")
    assert record.zone_ids == [zone.id]  # type: ignore[attr-defined]


# --- the seeded stories ------------------------------------------------------------------------


async def test_seeded_stories_create_exactly_the_predicted_notifications(
    session: AsyncSession,
) -> None:
    summary = await seed(session, today=TODAY, days=14, rng_seed=42)
    assert await evaluate(session) == 4
    alerts = {(field, user, code): alert_id for alert_id, field, user, code, _ in summary.alerts}
    got = [
        (n.alert_id, n.forecast_date, n.affected_area, n.probability)
        for n in await notifications(session)
    ]
    assert got == [
        (alerts["Los Álamos", "ana", "frost"], day(4), Decimal("0.4"), 80),
        (alerts["Los Álamos", "ana", "frost"], day(5), Decimal(1), 90),
        (alerts["El Rincón", "bruno", "rain"], day(3), Decimal(1), 70),
        (alerts["La Compartida", "ana", "heat"], day(2), Decimal(1), 70),
    ]


async def test_the_readme_walkthrough_alert_fires_on_the_two_pinned_dates(
    session: AsyncSession,
) -> None:
    await seed(session, today=TODAY, days=14, rng_seed=42)
    await evaluate(session)
    ana = (await session.execute(select(User).where(User.first_name == "Ana"))).scalar_one()
    esperanza = (
        await session.execute(select(Field).where(Field.name == "La Esperanza"))
    ).scalar_one()
    alert = await make_alert(session, ana, esperanza, "frost", 70)
    assert await evaluate(session) == 2
    rows = [n for n in await notifications(session) if n.alert_id == alert.id]
    assert [(n.forecast_date, n.probability) for n in rows] == [(day(2), 85), (day(3), 72)]
