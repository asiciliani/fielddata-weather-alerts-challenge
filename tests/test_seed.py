from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import seed as seed_module
from app.models import (
    Alert,
    EventType,
    Field,
    FieldMember,
    FieldZone,
    User,
    WeatherForecast,
    Zone,
)
from app.seed import FIELDS, WATCHED_MAX, ZONES, seed, watched_pairs

TODAY = date(2026, 11, 13)
DAYS = 14


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _forecasts(session: AsyncSession) -> dict[tuple[str, str, str, date], int]:
    rows = await session.execute(
        select(
            Zone.latitude,
            Zone.longitude,
            EventType.code,
            WeatherForecast.forecast_date,
            WeatherForecast.probability,
        )
        .join(Zone, Zone.id == WeatherForecast.zone_id)
        .join(EventType, EventType.id == WeatherForecast.event_type_id)
    )
    return {(str(lat), str(lon), code, day): p for lat, lon, code, day, p in rows.all()}


async def test_row_counts(session: AsyncSession) -> None:
    summary = await seed(session, today=TODAY, days=DAYS, rng_seed=42)
    assert await _count(session, User) == 2
    assert await _count(session, Field) == 4
    assert await _count(session, FieldMember) == 5
    assert await _count(session, Zone) == 6
    assert await _count(session, FieldZone) == 6
    assert await _count(session, Alert) == 4
    assert await _count(session, WeatherForecast) == 6 * 4 * DAYS == summary.forecasts
    assert len(summary.alerts) == 4 and len(summary.fields) == 4


async def test_coverages_add_up_to_one_per_field(session: AsyncSession) -> None:
    await seed(session, today=TODAY, days=DAYS, rng_seed=42)
    sums = (
        await session.execute(
            select(FieldZone.field_id, func.sum(FieldZone.coverage)).group_by(FieldZone.field_id)
        )
    ).all()
    assert len(sums) == 4
    assert all(total == Decimal(1) for _, total in sums)


async def test_pinned_and_background_values(session: AsyncSession) -> None:
    await seed(session, today=TODAY, days=DAYS, rng_seed=42)
    forecasts = await _forecasts(session)
    assert all(0 <= p <= 100 for p in forecasts.values())
    pinned = set()
    for story in FIELDS:
        for zone, code, offset, probability in story.pins:
            key = (*ZONES[zone], code, TODAY + timedelta(days=offset))
            assert forecasts[key] == probability
            pinned.add(key)
    for zone, code in watched_pairs():
        for offset in range(DAYS):
            key = (*ZONES[zone], code, TODAY + timedelta(days=offset))
            if key not in pinned:
                assert forecasts[key] <= WATCHED_MAX < 50


async def test_running_twice_duplicates_nothing_and_restores_values(
    session: AsyncSession,
) -> None:
    await seed(session, today=TODAY, days=DAYS, rng_seed=42)
    first = await _forecasts(session)
    await session.execute(update(WeatherForecast).values(probability=0))
    await session.execute(update(Alert).values(threshold=99, is_active=False))
    await seed(session, today=TODAY, days=DAYS, rng_seed=42)
    session.expire_all()
    assert await _count(session, User) == 2
    assert await _count(session, Field) == 4
    assert await _count(session, Alert) == 4
    assert await _forecasts(session) == first
    alerts = (await session.execute(select(Alert))).scalars().all()
    assert all(a.is_active and a.threshold < 99 for a in alerts)


async def test_same_seed_same_data(session: AsyncSession) -> None:
    await seed(session, today=TODAY, days=DAYS, rng_seed=7)
    first = await _forecasts(session)
    await seed(session, today=TODAY, days=DAYS, rng_seed=8)
    assert await _forecasts(session) != first
    await seed(session, today=TODAY, days=DAYS, rng_seed=7)
    assert await _forecasts(session) == first


def test_main_rejects_zero_days() -> None:
    with pytest.raises(SystemExit):
        seed_module.main(["--days", "0"])


def test_main_prints_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = seed_module.SeedSummary(
        users=[(1, "Ana Gómez")],
        fields=[(1, "La Esperanza", "ana (owner)")],
        alerts=[(1, "Los Álamos", "ana", "frost", 70)],
        forecasts=336,
    )

    async def fake_run(days: int, rng_seed: int) -> seed_module.SeedSummary:
        assert (days, rng_seed) == (3, 5)
        return summary

    monkeypatch.setattr(seed_module, "_run", fake_run)
    seed_module.main(["--days", "3", "--seed", "5"])
    out = capsys.readouterr().out
    assert "La Esperanza" in out and "frost >= 70%" in out and "336" in out
