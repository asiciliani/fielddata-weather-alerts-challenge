"""Identifiers outside the BIGINT range are rejected before any query (never a 500)."""

import inspect
import logging
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import make_user

HUGE = str(10**19)


async def test_huge_identity_is_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/fields", headers={"X-User-Id": HUGE})
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", f"/api/v1/alerts/{HUGE}", None),
        ("PATCH", f"/api/v1/alerts/{HUGE}", {"threshold": 50}),
        ("DELETE", f"/api/v1/alerts/{HUGE}", None),
        ("GET", f"/api/v1/fields/{HUGE}/outlook", None),
        ("GET", f"/api/v1/alerts?field_id={HUGE}", None),
        ("GET", f"/api/v1/notifications?alert_id={HUGE}", None),
        ("GET", f"/api/v1/notifications?field_id={HUGE}", None),
        ("GET", f"/api/v1/alerts?offset={HUGE}", None),
        ("POST", "/api/v1/alerts", {"field_id": 10**19, "event_type": "frost", "threshold": 70}),
    ],
)
async def test_huge_ids_are_422_not_500(
    client: AsyncClient,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
    body: object,
) -> None:
    user = await make_user(session)
    await session.commit()
    with caplog.at_level(logging.ERROR):
        r = await client.request(method, path, json=body, headers={"X-User-Id": str(user.id)})
    assert r.status_code == 422
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]


def _api_routes(routes: list[Any]) -> list[APIRoute]:
    """APIRoutes, also inside included routers (kept unflattened since FastAPI 0.16x)."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            found.extend(_api_routes(route.original_router.routes))
    return found


def _dependency_calls(dependant: Any) -> list[Any]:
    calls = []
    for dep in dependant.dependencies:
        calls.append(dep.call)
        calls.extend(_dependency_calls(dep))
    return calls


def test_every_route_dependency_is_async(client: AsyncClient) -> None:
    """Concurrency rule 1: nothing relies on FastAPI's thread pool for sync dependencies."""
    routes = _api_routes(client.app.routes)  # type: ignore[attr-defined]
    assert len(routes) == 12
    for route in routes:
        assert inspect.iscoroutinefunction(route.endpoint), route.path
        for call in _dependency_calls(route.dependant):
            is_async = inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)
            assert is_async, (route.path, call)
