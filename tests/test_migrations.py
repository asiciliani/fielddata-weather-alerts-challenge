"""Both revisions go up, down and up again; rolling back the feature drops only its tables."""

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import run_alembic

STAND_INS = {"users", "fields", "field_members", "zones", "field_zones", "weather_forecasts"}
FEATURE = {"event_types", "alerts", "notifications"}


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: inspect(c).get_table_names())
    finally:
        await engine.dispose()
    return set(names) - {"alembic_version"}


async def test_feature_rollback_drops_only_its_own_tables(scratch_database_url: str) -> None:
    run_alembic("upgrade", "head", database_url=scratch_database_url)
    assert await _tables(scratch_database_url) == STAND_INS | FEATURE

    run_alembic("downgrade", "0001_agrobot_stand_ins", database_url=scratch_database_url)
    assert await _tables(scratch_database_url) == STAND_INS

    run_alembic("downgrade", "base", database_url=scratch_database_url)
    assert await _tables(scratch_database_url) == set()

    run_alembic("upgrade", "head", database_url=scratch_database_url)
    assert await _tables(scratch_database_url) == STAND_INS | FEATURE
