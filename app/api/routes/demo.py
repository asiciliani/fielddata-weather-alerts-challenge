"""The demo page (``/demo``) and the read-only outlook endpoint it uses.

The page renders server results only: whether an alert fires on a date, and with what affected
area, comes from ``firing_matches``, the same rule evaluation uses.
"""

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, PathId, SessionDep
from app.db import database_now
from app.errors import NotFound
from app.models import Alert, EventType, FieldMember, FieldZone, WeatherForecast, Zone
from app.models import Field as FieldModel
from app.schemas import FieldOutlook, OutlookAlert, OutlookDate, OutlookForecast, OutlookZone
from app.services.evaluation import firing_matches, window_end

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "demo"
# The smallest coverage the schema can store: every date with any zone at the threshold.
_ANY_AREA = Decimal("0.0001")

page_router = APIRouter(include_in_schema=False)
router = APIRouter(prefix="/fields", tags=["fields"])


@page_router.get("/demo")
async def demo_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@router.get("/{field_id}/outlook")
async def field_outlook(
    field_id: PathId, request: Request, session: SessionDep, user: CurrentUser
) -> FieldOutlook:
    """The field's zones, its forecasts for the window, and per date whether each of the
    caller's alerts on it fires (used by the demo page)."""
    member = (
        await session.execute(
            sa.select(FieldModel.name, FieldMember.role)
            .join(FieldMember, FieldMember.field_id == FieldModel.id)
            .where(FieldModel.id == field_id, FieldMember.user_id == user.id)
        )
    ).one_or_none()
    if member is None:
        raise NotFound("field")
    settings = request.app.state.settings
    today = (await database_now(session)).astimezone(settings.tz).date()
    end_date = window_end(today, settings.evaluation_horizon_days)

    zones = [
        OutlookZone(id=z.id, latitude=z.latitude, longitude=z.longitude, coverage=coverage)
        for z, coverage in (
            await session.execute(
                sa.select(Zone, FieldZone.coverage)
                .join(FieldZone, FieldZone.zone_id == Zone.id)
                .where(FieldZone.field_id == field_id)
                .order_by(Zone.latitude.desc(), Zone.longitude)
            )
        ).all()
    ]
    forecasts = [
        OutlookForecast(event_type=code, zone_id=zone_id, forecast_date=day, probability=p)
        for code, zone_id, day, p in (
            await session.execute(
                sa.select(
                    EventType.code,
                    WeatherForecast.zone_id,
                    WeatherForecast.forecast_date,
                    WeatherForecast.probability,
                )
                .join(EventType, EventType.id == WeatherForecast.event_type_id)
                .join(FieldZone, FieldZone.zone_id == WeatherForecast.zone_id)
                .where(
                    FieldZone.field_id == field_id,
                    WeatherForecast.forecast_date.between(today, end_date),
                )
                .order_by(EventType.id, WeatherForecast.zone_id, WeatherForecast.forecast_date)
            )
        ).all()
    ]

    alerts = (
        (
            await session.execute(
                sa.select(Alert)
                .where(Alert.field_id == field_id, Alert.user_id == user.id)
                .order_by(Alert.id)
            )
        )
        .scalars()
        .all()
    )
    dates: dict[int, list[OutlookDate]] = defaultdict(list)
    if alerts:
        matches = firing_matches(
            today=today,
            end_date=end_date,
            min_affected_area=_ANY_AREA,
            where=[Alert.id.in_([a.id for a in alerts])],
        )
        for m in (await session.execute(matches)).all():
            dates[m.alert_id].append(
                OutlookDate(
                    forecast_date=m.forecast_date,
                    affected_area=m.affected_area,
                    probability=m.probability,
                    fires=m.affected_area >= settings.alert_min_affected_area,
                )
            )
    return FieldOutlook(
        id=field_id,
        name=member.name,
        role=member.role,
        today=today,
        end_date=end_date,
        min_affected_area=settings.alert_min_affected_area,
        zones=zones,
        forecasts=forecasts,
        alerts=[
            OutlookAlert(
                id=a.id,
                event_type=a.event_type.code,
                threshold=a.threshold,
                is_active=a.is_active,
                dates=sorted(dates[a.id], key=lambda d: d.forecast_date),
            )
            for a in alerts
        ],
    )
