"""Alert create, read, list, change and delete, always scoped to the calling user.

Services never commit: the route commits after the service returns.
"""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AlertAlreadyExists, NotFound, UnknownEventType
from app.models import Alert, EventType, FieldMember, FieldZone, User
from app.schemas import AlertCreate, AlertRead, AlertUpdate

_DUPLICATE_ALERT = "uq_alerts_user_id_field_id_event_type_id"


def _zones_mapped(field_id: Any) -> sa.Exists:
    return sa.exists().where(FieldZone.field_id == field_id)


def _read(alert: Alert, zones_mapped: bool) -> AlertRead:
    return AlertRead(
        id=alert.id,
        field_id=alert.field_id,
        event_type=alert.event_type.code,
        threshold=alert.threshold,
        is_active=alert.is_active,
        zones_mapped=zones_mapped,
        created_at=alert.created_at,
        updated_at=alert.updated_at,
    )


async def _read_one(session: AsyncSession, alert: Alert) -> AlertRead:
    mapped = (await session.execute(sa.select(_zones_mapped(alert.field_id)))).scalar_one()
    return _read(alert, mapped)


async def get_event_type(session: AsyncSession, code: str) -> EventType:
    event_type = (
        await session.execute(sa.select(EventType).where(EventType.code == code))
    ).scalar_one_or_none()
    if event_type is None:
        raise UnknownEventType(code)
    return event_type


async def is_member(session: AsyncSession, user: User, field_id: int) -> bool:
    return await session.get(FieldMember, (user.id, field_id)) is not None


async def _owned_alert(session: AsyncSession, user: User, alert_id: int) -> Alert:
    alert = (
        await session.execute(
            sa.select(Alert).where(Alert.id == alert_id, Alert.user_id == user.id)
        )
    ).scalar_one_or_none()
    if alert is None:
        raise NotFound("alert")  # also for another user's alert: existence is not revealed
    return alert


async def create_alert(session: AsyncSession, user: User, data: AlertCreate) -> AlertRead:
    """Create an alert for the caller on a field they are a member of.

    Duplicates are detected by the database's uniqueness rule, not by a prior read; a membership
    removed between the check and the insert fails the composite foreign key and is a 404 too.
    """
    if not await is_member(session, user, data.field_id):
        raise NotFound("field")
    event_type = await get_event_type(session, data.event_type)
    alert = Alert(
        user_id=user.id,
        field_id=data.field_id,
        event_type=event_type,
        threshold=data.threshold,
        is_active=data.is_active,
    )
    session.add(alert)
    try:
        await session.flush()
    except IntegrityError as exc:
        cause = exc.orig.__cause__ if exc.orig is not None else None
        constraint = getattr(cause, "constraint_name", None)
        if constraint == _DUPLICATE_ALERT:
            raise AlertAlreadyExists() from exc
        if getattr(exc.orig, "sqlstate", None) == "23503":  # foreign key: membership or field gone
            raise NotFound("field") from exc
        raise
    return await _read_one(session, alert)


async def get_alert(session: AsyncSession, user: User, alert_id: int) -> AlertRead:
    return await _read_one(session, await _owned_alert(session, user, alert_id))


async def list_alerts(
    session: AsyncSession,
    user: User,
    *,
    field_id: int | None,
    event_type: str | None,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> list[AlertRead]:
    query = sa.select(Alert, _zones_mapped(Alert.field_id).label("zones_mapped")).where(
        Alert.user_id == user.id
    )
    if field_id is not None:
        query = query.where(Alert.field_id == field_id)
    if event_type is not None:
        query = query.where(Alert.event_type_id == (await get_event_type(session, event_type)).id)
    if is_active is not None:
        query = query.where(Alert.is_active == is_active)
    rows = await session.execute(query.order_by(Alert.id).limit(limit).offset(offset))
    return [_read(alert, mapped) for alert, mapped in rows.all()]


async def update_alert(
    session: AsyncSession, user: User, alert_id: int, data: AlertUpdate
) -> AlertRead:
    """Change the threshold or the active flag.

    The UPDATE waits for the alert's row lock while a dispatcher is sending it; after the
    connection's lock timeout it fails, and the API answers 503.
    """
    alert = await _owned_alert(session, user, alert_id)
    for name, value in data.changes().items():
        setattr(alert, name, value)
    await session.flush()
    await session.refresh(alert, attribute_names=["updated_at"])
    return await _read_one(session, alert)


async def delete_alert(session: AsyncSession, user: User, alert_id: int) -> None:
    """Delete the alert; its notifications go with it (ON DELETE CASCADE)."""
    alert = await _owned_alert(session, user, alert_id)
    await session.execute(sa.delete(Alert).where(Alert.id == alert.id))
