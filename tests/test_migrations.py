"""Both revisions go up, down and up again; rolling back the feature drops only its tables."""

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.models import Base
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


async def test_database_constraint_names_match_the_models(engine: AsyncEngine) -> None:
    """`alembic check` does not compare CHECK constraint names; this does, for every kind."""
    expected = set()
    for table in Base.metadata.tables.values():
        expected |= {c.name for c in table.constraints}
        expected |= {i.name for i in table.indexes}
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
                "UNION SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
        )
        actual = {name for (name,) in rows} - {"alembic_version_pkc"}
    # Unique and primary keys are also listed as indexes under the same name.
    assert actual == expected
