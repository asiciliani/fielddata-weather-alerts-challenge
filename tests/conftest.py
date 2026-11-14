"""Shared fixtures. Tests run against a real PostgreSQL test database (see the Makefile).

The schema is created by running the Alembic migrations once per test session; every table
except ``event_types`` (seeded by the migration) is emptied after each test.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import build_engine, build_session_factory
from app.main import create_app
from app.models import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    f"postgresql+asyncpg://agrobot:agrobot@localhost:{os.environ.get('DB_PORT', '5432')}"
    "/agrobot_test",
)
_TRUNCATED_TABLES = ", ".join(t for t in Base.metadata.tables if t != "event_types")


def run_alembic(*args: str, database_url: str = TEST_DATABASE_URL) -> None:
    """Run Alembic in a subprocess: its async env calls ``asyncio.run``, which cannot nest."""
    subprocess.run(  # noqa: ASYNC221 - only ever called from sync fixtures and tests
        [sys.executable, "-m", "alembic", *args],
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    return Settings(_env_file=None, database_url=TEST_DATABASE_URL, log_format="console")


@pytest.fixture(scope="session")
def migrated_database() -> None:
    database = make_url(TEST_DATABASE_URL).database or ""
    assert database.endswith("_test"), f"refusing to run tests against {database!r}"
    run_alembic("upgrade", "head")


@pytest.fixture(scope="session")
async def engine(test_settings: Settings, migrated_database: None) -> AsyncIterator[AsyncEngine]:
    engine = build_engine(test_settings)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_session_factory(engine)


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
        assert name.endswith("_test"), f"refusing to truncate {name!r}"
        await conn.execute(text(f"TRUNCATE {_TRUNCATED_TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="session")
async def _clean_start(engine: AsyncEngine) -> None:
    await _truncate(engine)  # a previous run may have been interrupted


@pytest.fixture(autouse=True)
async def _clean_tables(engine: AsyncEngine, _clean_start: None) -> AsyncIterator[None]:
    yield
    await _truncate(engine)


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(test_settings: Settings, migrated_database: None) -> AsyncIterator[AsyncClient]:
    app = create_app(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        http.app = app  # type: ignore[attr-defined]
        yield http
    await app.state.engine.dispose()


@pytest.fixture
async def scratch_database_url(engine: AsyncEngine) -> AsyncIterator[str]:
    """An empty throwaway database, for migration tests that go down and up again."""
    name = "agrobot_migration_test"
    admin = engine.execution_options(isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        await conn.execute(text(f"CREATE DATABASE {name}"))
    yield make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    async with admin.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
