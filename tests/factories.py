"""Small async helpers that create one row each, flush it and return the ORM object."""

import itertools
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import FieldRole, NotificationStatus
from app.models import (
    Alert,
    EventType,
    Field,
    FieldMember,
    FieldZone,
    Notification,
    User,
    WeatherForecast,
    Zone,
)

_counter = itertools.count(1)


async def event_type(session: AsyncSession, code: str) -> EventType:
    return (await session.execute(select(EventType).where(EventType.code == code))).scalar_one()


async def make_user(session: AsyncSession, first_name: str = "Ana", **kw: object) -> User:
    n = next(_counter)
    user = User(
        first_name=first_name,
        last_name=str(kw.pop("last_name", "Test")),
        phone_number=str(kw.pop("phone_number", f"+54911{n:08d}")),
    )
    session.add(user)
    await session.flush()
    return user


async def make_zone(
    session: AsyncSession, latitude: str | None = None, longitude: str | None = None
) -> Zone:
    n = next(_counter)
    zone = Zone(
        latitude=Decimal(latitude) if latitude else Decimal("-34.125") - Decimal(n) / 4,
        longitude=Decimal(longitude) if longitude else Decimal("-60.125"),
    )
    session.add(zone)
    await session.flush()
    return zone


async def make_field(
    session: AsyncSession,
    name: str = "La Esperanza",
    zones: list[tuple[Zone, str | Decimal]] | None = None,
) -> Field:
    """A field, linked to ``zones`` as ``(zone, coverage)`` pairs."""
    field = Field(name=name)
    session.add(field)
    await session.flush()
    for zone, coverage in zones or []:
        session.add(FieldZone(field_id=field.id, zone_id=zone.id, coverage=Decimal(coverage)))
    await session.flush()
    return field


async def add_member(
    session: AsyncSession, user: User, field: Field, role: FieldRole = FieldRole.OWNER
) -> FieldMember:
    member = FieldMember(user_id=user.id, field_id=field.id, role=role)
    session.add(member)
    await session.flush()
    return member


async def make_forecast(
    session: AsyncSession, zone: Zone, code: str, forecast_date: date, probability: int
) -> WeatherForecast:
    forecast = WeatherForecast(
        zone_id=zone.id,
        event_type_id=(await event_type(session, code)).id,
        forecast_date=forecast_date,
        probability=probability,
    )
    session.add(forecast)
    await session.flush()
    return forecast


async def make_alert(
    session: AsyncSession,
    user: User,
    field: Field,
    code: str = "frost",
    threshold: int = 70,
    *,
    is_active: bool = True,
) -> Alert:
    """An alert; the user must already be a member of the field (see ``add_member``)."""
    alert = Alert(
        user_id=user.id,
        field_id=field.id,
        event_type=await event_type(session, code),
        threshold=threshold,
        is_active=is_active,
    )
    session.add(alert)
    await session.flush()
    return alert


async def make_notification(
    session: AsyncSession,
    alert: Alert,
    forecast_date: date,
    *,
    next_attempt_at: datetime,
    status: NotificationStatus = NotificationStatus.PENDING,
    forecast: WeatherForecast | None = None,
    threshold: int | None = None,
    probability: int = 80,
    affected_area: str | Decimal = "1",
    attempts: int = 0,
    sent_at: datetime | None = None,
) -> Notification:
    notification = Notification(
        alert_id=alert.id,
        forecast_id=forecast.id if forecast else None,
        forecast_date=forecast_date,
        threshold=threshold if threshold is not None else alert.threshold,
        probability=probability,
        affected_area=Decimal(affected_area),
        status=status,
        attempts=attempts,
        next_attempt_at=next_attempt_at,
        sent_at=sent_at if sent_at or status is not NotificationStatus.SENT else datetime.now(UTC),
    )
    session.add(notification)
    await session.flush()
    return notification
