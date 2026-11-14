"""Shared fixtures. Tests run against a real PostgreSQL test database (see the Makefile)."""

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://agrobot:agrobot@localhost:5432/agrobot_test"
)


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    return Settings(_env_file=None, database_url=TEST_DATABASE_URL, log_format="console")


@pytest.fixture
async def client(test_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        http.app = app  # type: ignore[attr-defined]
        yield http
    await app.state.engine.dispose()
