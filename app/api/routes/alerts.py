"""The caller's alerts. Field and event type are fixed for an alert's life."""

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response, status

from app.api.deps import CurrentUser, PaginationDep, PathId, QueryId, SessionDep
from app.schemas import AlertCreate, AlertRead, AlertUpdate, Page
from app.services import alerts as service

router = APIRouter(prefix="/alerts", tags=["alerts"])

_BUSY: dict[int | str, dict[str, Any]] = {
    503: {"description": "the alert is being sent; retry after `Retry-After` seconds"}
}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_alert(
    body: AlertCreate,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
    response: Response,
) -> AlertRead:
    alert = await service.create_alert(session, user, body)
    await session.commit()
    response.headers["Location"] = request.app.url_path_for("get_alert", alert_id=alert.id)
    return alert


@router.get("")
async def list_alerts(
    session: SessionDep,
    user: CurrentUser,
    page: PaginationDep,
    field_id: QueryId = None,
    event_type: Annotated[str | None, Query(max_length=32)] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[AlertRead]:
    items = await service.list_alerts(
        session,
        user,
        field_id=field_id,
        event_type=event_type,
        is_active=is_active,
        limit=page.limit,
        offset=page.offset,
    )
    return Page(items=items, limit=page.limit, offset=page.offset)


@router.get("/{alert_id}")
async def get_alert(alert_id: PathId, session: SessionDep, user: CurrentUser) -> AlertRead:
    return await service.get_alert(session, user, alert_id)


@router.patch("/{alert_id}", responses=_BUSY)
async def update_alert(
    alert_id: PathId, body: AlertUpdate, session: SessionDep, user: CurrentUser
) -> AlertRead:
    alert = await service.update_alert(session, user, alert_id, body)
    await session.commit()
    return alert


@router.delete("/{alert_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_BUSY)
async def delete_alert(alert_id: PathId, session: SessionDep, user: CurrentUser) -> None:
    await service.delete_alert(session, user, alert_id)
    await session.commit()
