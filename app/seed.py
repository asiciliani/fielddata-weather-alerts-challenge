"""Demo data standing in for what Agrobot already has: users, fields, zones, forecasts.

Run with ``python -m app.seed [--days 14] [--seed 42]``. Idempotent: running it again updates the
same rows (forecasts are upserted the way the ingestion job is assumed to write them).

Each field tells one story; pinned forecast dates are relative to today in the forecast timezone:

* La Esperanza: one zone; two cold dates, which the README walkthrough's alert turns into one
  message.
* Los Álamos: 60% / 40% across two zones; fires "(40% del campo)" when only one zone is cold.
* El Rincón: 95% / 5%; rain in the 5% sliver does not fire, rain in both zones does.
* La Compartida: shared by two users with thresholds 60% and 80% on a 70% forecast; only the
  first is notified.

Background forecasts are random (seeded). For every (zone, event type) that an alert watches,
including the walkthrough's, they stay below 50 so that only the pinned dates fire.
"""

import argparse
import asyncio
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import build_engine, build_session_factory
from app.enums import FieldRole
from app.models import Alert, EventType, Field, FieldMember, FieldZone, User, WeatherForecast, Zone

USERS = {
    "ana": ("Ana", "Gómez", "+5491100000001"),
    "bruno": ("Bruno", "Díaz", "+5491100000002"),
}

# 0.25-degree cell centres in Buenos Aires province (4 decimals, as the provider reports them).
ZONES = {
    "Z1": ("-34.6250", "-59.3750"),
    "Z2": ("-35.1250", "-60.1250"),
    "Z3": ("-35.1250", "-59.8750"),
    "Z4": ("-36.3750", "-61.6250"),
    "Z5": ("-36.1250", "-61.6250"),
    "Z6": ("-34.8750", "-58.8750"),
}


@dataclass(frozen=True)
class FieldStory:
    name: str
    zones: dict[str, str]  # zone key -> coverage
    members: dict[str, FieldRole]
    alerts: dict[str, tuple[str, int]]  # user key -> (event type code, threshold)
    pins: list[tuple[str, str, int, int]] = field(default_factory=list)  # zone, type, day, prob
    walkthrough: tuple[str, str] | None = None  # (zone, type) the README alert will watch


FIELDS = [
    FieldStory(
        name="La Esperanza",
        zones={"Z1": "1"},
        members={"ana": FieldRole.OWNER},
        alerts={},
        pins=[("Z1", "frost", 2, 85), ("Z1", "frost", 3, 72)],
        walkthrough=("Z1", "frost"),
    ),
    FieldStory(
        name="Los Álamos",
        zones={"Z2": "0.6", "Z3": "0.4"},
        members={"ana": FieldRole.OWNER},
        alerts={"ana": ("frost", 70)},
        pins=[("Z2", "frost", 4, 40), ("Z3", "frost", 4, 80)]
        + [("Z2", "frost", 5, 75), ("Z3", "frost", 5, 90)],
    ),
    FieldStory(
        name="El Rincón",
        zones={"Z4": "0.95", "Z5": "0.05"},
        members={"bruno": FieldRole.OWNER},
        alerts={"bruno": ("rain", 60)},
        pins=[("Z4", "rain", 1, 20), ("Z5", "rain", 1, 90)]
        + [("Z4", "rain", 3, 70), ("Z5", "rain", 3, 70)],
    ),
    FieldStory(
        name="La Compartida",
        zones={"Z6": "1"},
        members={"ana": FieldRole.OWNER, "bruno": FieldRole.AGRONOMIST},
        alerts={"ana": ("heat", 60), "bruno": ("heat", 80)},
        pins=[("Z6", "heat", 2, 70)],
    ),
]

WATCHED_MAX = 49  # background ceiling for watched (zone, event type) pairs


@dataclass
class SeedSummary:
    users: list[tuple[int, str]]
    fields: list[tuple[int, str, str]]  # id, name, members
    alerts: list[tuple[int, str, str, str, int]]  # id, field, user, event type, threshold
    forecasts: int


def watched_pairs() -> set[tuple[str, str]]:
    """(zone, event type) pairs some seeded or walkthrough alert watches."""
    pairs: set[tuple[str, str]] = set()
    for story in FIELDS:
        for code, _ in story.alerts.values():
            pairs.update((zone, code) for zone in story.zones)
        if story.walkthrough:
            pairs.add(story.walkthrough)
    return pairs


async def _upsert_users(session: AsyncSession) -> dict[str, int]:
    ids: dict[str, int] = {}
    for key, (first, last, phone) in USERS.items():
        stmt = insert(User).values(first_name=first, last_name=last, phone_number=phone)
        upsert = stmt.on_conflict_do_update(
            index_elements=[User.phone_number],
            set_={"first_name": stmt.excluded.first_name, "last_name": stmt.excluded.last_name},
        ).returning(User.id)
        ids[key] = (await session.execute(upsert)).scalar_one()
    return ids


async def _upsert_zones(session: AsyncSession) -> dict[str, int]:
    ids: dict[str, int] = {}
    for key, (lat, lon) in ZONES.items():
        values = {"latitude": Decimal(lat), "longitude": Decimal(lon)}
        await session.execute(insert(Zone).values(**values).on_conflict_do_nothing())
        ids[key] = (await session.execute(sa.select(Zone.id).filter_by(**values))).scalar_one()
    return ids


async def _field_id(session: AsyncSession, name: str) -> int:
    existing = (
        (await session.execute(sa.select(Field.id).where(Field.name == name).order_by(Field.id)))
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    return (
        await session.execute(sa.insert(Field).values(name=name).returning(Field.id))
    ).scalar_one()


async def _upsert_forecasts(session: AsyncSession, rows: list[dict[str, object]]) -> None:
    """Upsert as the ingestion job is assumed to: only ``probability`` and ``updated_at``."""
    stmt = insert(WeatherForecast)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["zone_id", "event_type_id", "forecast_date"],
            set_={"probability": stmt.excluded.probability, "updated_at": sa.func.now()},
        ),
        rows,
    )


async def seed(session: AsyncSession, *, today: date, days: int, rng_seed: int) -> SeedSummary:
    """Write the demo data in the caller's transaction (it does not commit)."""
    types = {
        code: type_id
        for code, type_id in (await session.execute(sa.select(EventType.code, EventType.id))).all()
    }
    user_ids = await _upsert_users(session)
    zone_ids = await _upsert_zones(session)

    summary = SeedSummary(users=[], fields=[], alerts=[], forecasts=0)
    summary.users = [(user_ids[k], f"{first} {last}") for k, (first, last, _) in USERS.items()]
    for story in FIELDS:
        field_id = await _field_id(session, story.name)
        for zone_key, coverage in story.zones.items():
            stmt = insert(FieldZone).values(
                field_id=field_id, zone_id=zone_ids[zone_key], coverage=Decimal(coverage)
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["field_id", "zone_id"],
                    set_={"coverage": stmt.excluded.coverage},
                )
            )
        for user_key, role in story.members.items():
            stmt = insert(FieldMember).values(
                user_id=user_ids[user_key], field_id=field_id, role=role
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["user_id", "field_id"], set_={"role": stmt.excluded.role}
                )
            )
        for user_key, (code, threshold) in story.alerts.items():
            stmt = insert(Alert).values(
                user_id=user_ids[user_key],
                field_id=field_id,
                event_type_id=types[code],
                threshold=threshold,
                is_active=True,
            )
            upsert = stmt.on_conflict_do_update(
                index_elements=["user_id", "field_id", "event_type_id"],
                set_={"threshold": threshold, "is_active": True, "updated_at": sa.func.now()},
            ).returning(Alert.id)
            alert_id = (await session.execute(upsert)).scalar_one()
            summary.alerts.append((alert_id, story.name, user_key, code, threshold))
        members = ", ".join(f"{u} ({r.value})" for u, r in story.members.items())
        summary.fields.append((field_id, story.name, members))

    rng = random.Random(rng_seed)
    watched = watched_pairs()
    probabilities: dict[tuple[str, str, date], int] = {}
    for zone_key in zone_ids:
        for code in sorted(types):
            ceiling = WATCHED_MAX if (zone_key, code) in watched else 100
            for offset in range(days):
                day = today + timedelta(days=offset)
                probabilities[zone_key, code, day] = rng.randint(0, ceiling)
    for story in FIELDS:  # pinned values win over the background ones
        for zone_key, code, offset, probability in story.pins:
            probabilities[zone_key, code, today + timedelta(days=offset)] = probability
    await _upsert_forecasts(
        session,
        [
            {
                "zone_id": zone_ids[zone_key],
                "event_type_id": types[code],
                "forecast_date": day,
                "probability": probability,
            }
            for (zone_key, code, day), probability in probabilities.items()
        ],
    )
    summary.forecasts = len(probabilities)
    return summary


async def _run(days: int, rng_seed: int) -> SeedSummary:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        async with build_session_factory(engine)() as session, session.begin():
            today = datetime.now(settings.tz).date()
            return await seed(session, today=today, days=days, rng_seed=rng_seed)
    finally:
        await engine.dispose()


def _print_summary(summary: SeedSummary) -> None:
    print("Users:")
    for user_id, name in summary.users:
        print(f"  {user_id:>3}  {name}")
    print("Fields:")
    for field_id, name, members in summary.fields:
        print(f"  {field_id:>3}  {name:<15} {members}")
    print("Alerts:")
    for alert_id, field_name, user, code, threshold in summary.alerts:
        print(f"  {alert_id:>3}  {field_name:<15} {user:<6} {code} >= {threshold}%")
    print(f"Forecasts written: {summary.forecasts}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Write the demo data (idempotent).")
    parser.add_argument("--days", type=int, default=14, help="days of forecasts from today")
    parser.add_argument("--seed", type=int, default=42, help="random seed for background values")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")
    _print_summary(asyncio.run(_run(args.days, args.seed)))


if __name__ == "__main__":
    main()
