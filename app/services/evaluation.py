"""Evaluation: turn forecasts into pending notifications with one set-based statement.

The matching rule (design doc, Alert semantics) is built once, by ``firing_matches``, and shared
with the dispatcher's re-check and the demo outlook. For one alert and one date in the window:
take the zones of the alert's field whose forecast for its event type reaches the threshold; the
affected area is the sum of their coverages; the alert fires when that area reaches the minimum,
reporting the affected forecast with the highest probability.

Correctness rests on the unique key on (alert, forecast date); the advisory lock only keeps
replicas from repeating the same work. Nothing here commits: the caller owns the transaction.
"""

import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import NotificationStatus
from app.models import Alert, FieldZone, Notification, WeatherForecast

logger = logging.getLogger(__name__)

# Key of the transaction-scoped advisory lock that lets one replica evaluate at a time.
EVALUATION_LOCK_KEY = 7_270_001


def window_end(today: date, horizon_days: int) -> date:
    """Last date of the window (today and the next ``horizon_days`` days, both included)."""
    return today + timedelta(days=horizon_days)


def firing_matches(
    *,
    today: date,
    end_date: date,
    min_affected_area: Decimal,
    where: Iterable[sa.ColumnElement[bool]] = (),
) -> sa.Select[tuple[int, int, int, date, int, Decimal]]:
    """The (alert, date) pairs that fire, one row each, with what the notification reports.

    Columns: ``alert_id``, ``threshold``, ``forecast_id``, ``forecast_date``, ``probability`` and
    ``affected_area``. ``where`` adds conditions to the inner join of alerts, field zones and
    forecasts (for example, only some alerts, or "not notified yet").
    """
    matches = (
        sa.select(
            Alert.id.label("alert_id"),
            Alert.threshold,
            WeatherForecast.id.label("forecast_id"),
            WeatherForecast.forecast_date,
            WeatherForecast.probability,
            FieldZone.coverage,
        )
        .join(FieldZone, FieldZone.field_id == Alert.field_id)
        .join(
            WeatherForecast,
            sa.and_(
                WeatherForecast.zone_id == FieldZone.zone_id,
                WeatherForecast.event_type_id == Alert.event_type_id,
            ),
        )
        .where(
            Alert.is_active,
            WeatherForecast.forecast_date.between(today, end_date),
            WeatherForecast.probability >= Alert.threshold,
            *where,
        )
        .cte("matches")
    )
    group = (matches.c.alert_id, matches.c.forecast_date)
    ranked = sa.select(
        matches,
        sa.func.least(sa.func.sum(matches.c.coverage).over(partition_by=group), 1).label(
            "affected_area"
        ),
        sa.func.row_number()
        .over(partition_by=group, order_by=(matches.c.probability.desc(), matches.c.forecast_id))
        .label("rn"),
    ).cte("ranked")
    return sa.select(
        ranked.c.alert_id,
        ranked.c.threshold,
        ranked.c.forecast_id,
        ranked.c.forecast_date,
        ranked.c.probability,
        ranked.c.affected_area,
    ).where(ranked.c.rn == 1, ranked.c.affected_area >= min_affected_area)


async def evaluate_alerts(
    session: AsyncSession, *, today: date, horizon_days: int, min_affected_area: Decimal
) -> int:
    """Insert one pending notification per firing (alert, date) not notified yet.

    Returns how many were created. A new notification takes the next-attempt time of its alert's
    pending notifications, so it joins their retry instead of triggering an early one.
    """
    existing = sa.alias(Notification.__table__, "x")
    not_notified = ~sa.exists().where(
        existing.c.alert_id == Alert.id, existing.c.forecast_date == WeatherForecast.forecast_date
    )
    firing = firing_matches(
        today=today,
        end_date=window_end(today, horizon_days),
        min_affected_area=min_affected_area,
        where=[not_notified],
    ).subquery("firing")
    pending = sa.alias(Notification.__table__, "p")
    alert_schedule = (
        sa.select(sa.func.max(pending.c.next_attempt_at))
        .where(
            pending.c.alert_id == firing.c.alert_id,
            pending.c.status == NotificationStatus.PENDING.value,
        )
        .scalar_subquery()
    )
    rows = sa.select(
        firing.c.alert_id,
        firing.c.forecast_id,
        firing.c.forecast_date,
        firing.c.threshold,
        firing.c.probability,
        firing.c.affected_area,
        sa.func.coalesce(alert_schedule, sa.func.now()),
    ).order_by(firing.c.alert_id, firing.c.forecast_date)  # same order in every run: no deadlock
    stmt = (
        insert(Notification)
        .from_select(
            [
                "alert_id",
                "forecast_id",
                "forecast_date",
                "threshold",
                "probability",
                "affected_area",
                "next_attempt_at",
            ],
            rows,
        )
        .on_conflict_do_nothing(index_elements=["alert_id", "forecast_date"])
        .returning(Notification.id)
    )
    return len((await session.execute(stmt)).scalars().all())


async def try_evaluation_lock(session: AsyncSession) -> bool:
    """Take the evaluation's advisory lock for the current transaction, without waiting."""
    stmt = sa.select(sa.func.pg_try_advisory_xact_lock(EVALUATION_LOCK_KEY))
    return bool((await session.execute(stmt)).scalar_one())


async def check_data(
    session: AsyncSession,
    *,
    now: datetime,
    today: date,
    horizon_days: int,
    max_age_hours: int,
) -> None:
    """Warn about input data that would make alerts silently never fire.

    ``alerts_without_zones``: active alerts on fields with no zones, and fields whose coverages
    do not add up to 100%. ``forecasts_stale``: zones watched by an active alert that miss a
    forecast for some date in the window, or whose forecasts are older than the maximum age.
    """
    active_alert_fields = sa.select(Alert.field_id).where(Alert.is_active)
    unmapped = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Alert)
            .where(Alert.is_active, ~sa.exists().where(FieldZone.field_id == Alert.field_id))
        )
    ).scalar_one()
    partial = (
        await session.execute(
            sa.select(sa.func.count()).select_from(
                sa.select(FieldZone.field_id)
                .group_by(FieldZone.field_id)
                .having(sa.func.sum(FieldZone.coverage) != 1)
                .subquery()
            )
        )
    ).scalar_one()
    if unmapped or partial:
        logger.warning(
            "alerts_without_zones",
            extra={"alerts_without_zones": unmapped, "fields_with_partial_coverage": partial},
        )

    end_date = window_end(today, horizon_days)
    watched = (
        sa.select(FieldZone.zone_id, Alert.event_type_id)
        .join(Alert, Alert.field_id == FieldZone.field_id)
        .where(Alert.field_id.in_(active_alert_fields), Alert.is_active)
        .distinct()
        .subquery("watched")
    )
    in_window = sa.and_(
        WeatherForecast.zone_id == watched.c.zone_id,
        WeatherForecast.event_type_id == watched.c.event_type_id,
        WeatherForecast.forecast_date.between(today, end_date),
    )
    per_pair = (
        sa.select(
            watched.c.zone_id,
            sa.func.count(WeatherForecast.id).label("dates"),
            sa.func.max(WeatherForecast.updated_at).label("newest"),
        )
        .select_from(watched)
        .outerjoin(WeatherForecast, in_window)
        .group_by(watched.c.zone_id, watched.c.event_type_id)
        .subquery("per_pair")
    )
    oldest_allowed = now - timedelta(hours=max_age_hours)
    stale_zone_ids = (
        (
            await session.execute(
                sa.select(per_pair.c.zone_id)
                .where(
                    sa.or_(
                        per_pair.c.dates < (end_date - today).days + 1,
                        per_pair.c.newest < oldest_allowed,
                    )
                )
                .distinct()
                .order_by(per_pair.c.zone_id)
            )
        )
        .scalars()
        .all()
    )
    if stale_zone_ids:
        logger.warning(
            "forecasts_stale",
            extra={"stale_zones": len(stale_zone_ids), "zone_ids": list(stale_zone_ids[:20])},
        )
