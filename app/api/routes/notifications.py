"""The caller's notifications: what was detected and whether it was delivered."""

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Query
from sqlalchemy.orm import contains_eager

from app.api.deps import CurrentUser, PaginationDep, QueryId, SessionDep
from app.enums import NotificationStatus
from app.models import Alert, Notification
from app.schemas import NotificationRead, Page

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(
    session: SessionDep,
    user: CurrentUser,
    page: PaginationDep,
    status: Annotated[NotificationStatus | None, Query()] = None,
    field_id: QueryId = None,
    alert_id: QueryId = None,
) -> Page[NotificationRead]:
    """Newest first. Scoped through the alert's owner: another member's notifications on the
    same field are not listed."""
    query = (
        sa.select(Notification)
        .join(Notification.alert)
        .options(contains_eager(Notification.alert))
        .where(Alert.user_id == user.id)
    )
    if status is not None:
        query = query.where(Notification.status == status)
    if field_id is not None:
        query = query.where(Alert.field_id == field_id)
    if alert_id is not None:
        query = query.where(Notification.alert_id == alert_id)
    query = query.order_by(Notification.id.desc()).limit(page.limit).offset(page.offset)
    rows = (await session.execute(query)).scalars().all()
    items = [
        NotificationRead(
            id=n.id,
            alert_id=n.alert_id,
            forecast_id=n.forecast_id,
            field_id=n.alert.field_id,
            event_type=n.alert.event_type.code,
            forecast_date=n.forecast_date,
            threshold=n.threshold,
            probability=n.probability,
            affected_area=n.affected_area,
            status=n.status,
            sent_at=n.sent_at,
            created_at=n.created_at,
        )
        for n in rows
    ]
    return Page(items=items, limit=page.limit, offset=page.offset)
