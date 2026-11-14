"""The event types an alert can watch."""

import sqlalchemy as sa
from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.models import EventType
from app.schemas import EventTypeRead

router = APIRouter(prefix="/event-types", tags=["event types"])


@router.get("")
async def list_event_types(session: SessionDep, _: CurrentUser) -> list[EventTypeRead]:
    rows = await session.execute(sa.select(EventType.code, EventType.name).order_by(EventType.id))
    return [EventTypeRead(code=code, name=name) for code, name in rows.all()]
