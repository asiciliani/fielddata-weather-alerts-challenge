"""The schema enforces the data rules it can express (design doc, Conceptual model)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import FieldRole, NotificationStatus
from app.models import Alert, EventType, FieldMember, Notification, WeatherForecast
from tests.factories import (
    add_member,
    event_type,
    make_alert,
    make_field,
    make_forecast,
    make_notification,
    make_user,
    make_zone,
)

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
DAY = date(2026, 11, 15)


async def _alert(session: AsyncSession) -> Alert:
    user = await make_user(session)
    field = await make_field(session)
    await add_member(session, user, field)
    return await make_alert(session, user, field)


async def _expect_violation(
    session: AsyncSession, constraint: str, sql: str, **params: object
) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        await session.execute(text(sql), params)
    assert constraint in str(exc_info.value)
    await session.rollback()


async def test_migration_creates_the_four_event_types(session: AsyncSession) -> None:
    rows = (await session.execute(select(EventType.code, EventType.name))).all()
    assert sorted(rows) == [
        ("frost", "helada"),
        ("heat", "calor extremo"),
        ("rain", "lluvia"),
        ("strong_wind", "viento fuerte"),
    ]


async def test_enum_values_are_stored_lowercase(session: AsyncSession) -> None:
    alert = await _alert(session)
    await make_notification(session, alert, DAY, next_attempt_at=NOW)
    assert (await session.execute(text("SELECT status FROM notifications"))).scalar_one() == (
        "pending"
    )
    assert (await session.execute(text("SELECT role FROM field_members"))).scalar_one() == "owner"


async def test_alert_requires_membership(session: AsyncSession) -> None:
    user = await make_user(session)
    field = await make_field(session)
    frost = await event_type(session, "frost")
    await _expect_violation(
        session,
        "fk_alerts_user_id_field_members",
        "INSERT INTO alerts (user_id, field_id, event_type_id, threshold) VALUES (:u, :f, :e, 70)",
        u=user.id,
        f=field.id,
        e=frost.id,
    )


async def test_one_alert_per_user_field_and_event_type(session: AsyncSession) -> None:
    alert = await _alert(session)
    await _expect_violation(
        session,
        "uq_alerts_user_id_field_id_event_type_id",
        "INSERT INTO alerts (user_id, field_id, event_type_id, threshold) VALUES (:u, :f, :e, 50)",
        u=alert.user_id,
        f=alert.field_id,
        e=alert.event_type_id,
    )


async def test_two_members_can_each_watch_the_same_event(session: AsyncSession) -> None:
    field = await make_field(session)
    for name in ("Ana", "Beto"):
        user = await make_user(session, name)
        await add_member(session, user, field)
        await make_alert(session, user, field, "frost")
    assert len((await session.execute(select(Alert))).scalars().all()) == 2


async def test_forecast_key_is_unique(session: AsyncSession) -> None:
    zone = await make_zone(session)
    forecast = await make_forecast(session, zone, "frost", DAY, 50)
    await _expect_violation(
        session,
        "uq_weather_forecasts_zone_id_event_type_id_forecast_date",
        "INSERT INTO weather_forecasts (zone_id, event_type_id, forecast_date, probability) "
        "VALUES (:z, :e, :d, 60)",
        z=zone.id,
        e=forecast.event_type_id,
        d=DAY,
    )


async def test_zone_coordinates_are_unique(session: AsyncSession) -> None:
    await make_zone(session, "-34.125", "-60.125")
    await _expect_violation(
        session,
        "uq_zones_latitude_longitude",
        "INSERT INTO zones (latitude, longitude) VALUES (-34.125, -60.125)",
    )


@pytest.mark.parametrize(("latitude", "longitude"), [(-90.5, 0), (90.5, 0), (0, -180.5), (0, 181)])
async def test_zone_coordinates_in_range(
    session: AsyncSession, latitude: float, longitude: float
) -> None:
    await _expect_violation(
        session,
        "range",
        "INSERT INTO zones (latitude, longitude) VALUES (:lat, :lon)",
        lat=latitude,
        lon=longitude,
    )


@pytest.mark.parametrize("probability", [-1, 101])
async def test_forecast_probability_range(session: AsyncSession, probability: int) -> None:
    zone = await make_zone(session)
    frost = await event_type(session, "frost")
    await _expect_violation(
        session,
        "ck_weather_forecasts_probability_range",
        "INSERT INTO weather_forecasts (zone_id, event_type_id, forecast_date, probability) "
        "VALUES (:z, :e, :d, :p)",
        z=zone.id,
        e=frost.id,
        d=DAY,
        p=probability,
    )


@pytest.mark.parametrize("threshold", [0, 101])
async def test_alert_threshold_range(session: AsyncSession, threshold: int) -> None:
    alert = await _alert(session)
    await _expect_violation(
        session,
        "ck_alerts_threshold_range",
        "UPDATE alerts SET threshold = :t WHERE id = :id",
        t=threshold,
        id=alert.id,
    )


@pytest.mark.parametrize("coverage", ["0", "1.5"])
async def test_coverage_range(session: AsyncSession, coverage: str) -> None:
    field = await make_field(session)
    zone = await make_zone(session)
    await _expect_violation(
        session,
        "ck_field_zones_coverage_range",
        "INSERT INTO field_zones (field_id, zone_id, coverage) "
        "VALUES (:f, :z, CAST(:c AS numeric))",
        f=field.id,
        z=zone.id,
        c=coverage,
    )


async def test_unknown_role_rejected(session: AsyncSession) -> None:
    user = await make_user(session)
    field = await make_field(session)
    await _expect_violation(
        session,
        "ck_field_members_role",
        "INSERT INTO field_members (user_id, field_id, role) VALUES (:u, :f, 'boss')",
        u=user.id,
        f=field.id,
    )


_NOTIFICATION_INSERT = (
    "INSERT INTO notifications (alert_id, forecast_date, threshold, probability, affected_area, "
    "status, attempts, sent_at) VALUES (:a, :d, :t, :p, CAST(:area AS numeric), :s, :n, :sent)"
)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"t": 0}, "ck_notifications_threshold_range"),
        ({"t": 101}, "ck_notifications_threshold_range"),
        ({"p": -1}, "ck_notifications_probability_range"),
        ({"p": 101}, "ck_notifications_probability_range"),
        ({"area": "0"}, "ck_notifications_affected_area_range"),
        ({"area": "1.5"}, "ck_notifications_affected_area_range"),
        ({"n": -1}, "ck_notifications_attempts_non_negative"),
        ({"s": "lost"}, "ck_notifications_status"),
        ({"s": "sent", "sent": None}, "ck_notifications_sent_has_timestamp"),
    ],
)
async def test_notification_checks(
    session: AsyncSession, overrides: dict[str, object], constraint: str
) -> None:
    alert = await _alert(session)
    params: dict[str, object] = {
        "a": alert.id,
        "d": DAY,
        "t": 70,
        "p": 80,
        "area": "1",
        "s": "pending",
        "n": 0,
        "sent": None,
    } | overrides
    await _expect_violation(session, constraint, _NOTIFICATION_INSERT, **params)


async def test_one_notification_per_alert_and_date(session: AsyncSession) -> None:
    alert = await _alert(session)
    await make_notification(session, alert, DAY, next_attempt_at=NOW)
    await _expect_violation(
        session,
        "uq_notifications_alert_id_forecast_date",
        _NOTIFICATION_INSERT,
        a=alert.id,
        d=DAY,
        t=70,
        p=80,
        area="1",
        s="pending",
        n=0,
        sent=None,
    )


async def test_notification_database_defaults(session: AsyncSession) -> None:
    alert = await _alert(session)
    await session.execute(
        text(
            "INSERT INTO notifications (alert_id, forecast_date, threshold, probability, "
            "affected_area) VALUES (:a, :d, 70, 80, 1)"
        ),
        {"a": alert.id, "d": DAY},
    )
    row = (await session.execute(select(Notification))).scalar_one()
    assert row.status is NotificationStatus.PENDING
    assert row.attempts == 0
    assert row.next_attempt_at is not None and row.created_at is not None
    assert row.sent_at is None and row.last_error is None


async def test_deleting_an_alert_deletes_its_notifications(session: AsyncSession) -> None:
    alert = await _alert(session)
    await make_notification(session, alert, DAY, next_attempt_at=NOW)
    await session.execute(delete(Alert).where(Alert.id == alert.id))
    assert (await session.execute(select(Notification))).first() is None


async def test_deleting_a_membership_deletes_that_users_alerts(session: AsyncSession) -> None:
    field = await make_field(session)
    owner, other = await make_user(session, "Ana"), await make_user(session, "Beto")
    await add_member(session, owner, field)
    await add_member(session, other, field, FieldRole.AGRONOMIST)
    await make_alert(session, owner, field)
    kept = await make_alert(session, other, field)
    await session.execute(delete(FieldMember).where(FieldMember.user_id == owner.id))
    remaining = (await session.execute(select(Alert.id))).scalars().all()
    assert remaining == [kept.id]


async def test_deleting_a_forecast_keeps_the_notification(session: AsyncSession) -> None:
    alert = await _alert(session)
    zone = await make_zone(session)
    forecast = await make_forecast(session, zone, "frost", DAY, 80)
    await make_notification(session, alert, DAY, next_attempt_at=NOW, forecast=forecast)
    await session.execute(delete(WeatherForecast).where(WeatherForecast.id == forecast.id))
    session.expire_all()
    notification = (await session.execute(select(Notification))).scalar_one()
    assert notification.forecast_id is None
    assert notification.probability == 80


async def test_event_type_in_use_cannot_be_deleted(session: AsyncSession) -> None:
    alert = await _alert(session)
    await _expect_violation(
        session,
        "fk_alerts_event_type_id_event_types",
        "DELETE FROM event_types WHERE id = :id",
        id=alert.event_type_id,
    )
