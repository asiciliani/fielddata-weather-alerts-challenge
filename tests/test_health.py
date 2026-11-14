import logging

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.api.deps import get_session
from app.config import Settings
from app.db import build_engine, build_session_factory


async def test_live(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_with_database(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class _BrokenSession:
    async def execute(self, *_: object, **__: object) -> None:
        raise ConnectionRefusedError("database down")


async def test_ready_returns_503_when_database_fails(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken_session():
        yield _BrokenSession()

    client.app.dependency_overrides[get_session] = broken_session  # type: ignore[attr-defined]
    with caplog.at_level(logging.WARNING):
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    failures = [r for r in caplog.records if r.message == "readiness_check_failed"]
    assert len(failures) == 1
    assert failures[0].levelno == logging.WARNING


async def test_connections_carry_session_timeouts(test_settings: Settings) -> None:
    engine = build_engine(test_settings)
    try:
        async with build_session_factory(engine)() as session:
            assert (await session.execute(text("SHOW lock_timeout"))).scalar_one() == "5s"
            assert (await session.execute(text("SHOW statement_timeout"))).scalar_one() == "30s"
            idle = (
                await session.execute(text("SHOW idle_in_transaction_session_timeout"))
            ).scalar_one()
            assert idle == "0"
    finally:
        await engine.dispose()


async def test_worker_connections_set_idle_timeout_and_keepalives(test_settings: Settings) -> None:
    engine = build_engine(test_settings, worker=True)
    try:
        async with build_session_factory(engine)() as session:
            shown = {
                name: (await session.execute(text(f"SHOW {name}"))).scalar_one()
                for name in (
                    "idle_in_transaction_session_timeout",
                    "lock_timeout",
                    "tcp_keepalives_idle",
                    "tcp_keepalives_interval",
                    "tcp_keepalives_count",
                )
            }
            assert shown == {
                "idle_in_transaction_session_timeout": "30s",
                "lock_timeout": "5s",
                "tcp_keepalives_idle": "10",
                "tcp_keepalives_interval": "5",
                "tcp_keepalives_count": "3",
            }
    finally:
        await engine.dispose()
