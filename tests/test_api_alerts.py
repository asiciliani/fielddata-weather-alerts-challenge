import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import get_current_user
from app.enums import FieldRole
from app.models import Alert, Field, Notification, User
from app.services import alerts as alerts_service
from tests.factories import (
    add_member,
    make_alert,
    make_field,
    make_notification,
    make_user,
    make_zone,
)


def auth(user: User) -> dict[str, str]:
    return {"X-User-Id": str(user.id)}


@dataclass
class World:
    ana: User
    beto: User
    carla: User
    field: Field
    unmapped: Field


@pytest.fixture
async def world(session: AsyncSession) -> World:
    """Ana owns La Esperanza (one zone); Beto is an agronomist there; Ana also owns an unmapped
    field; Carla is a member of nothing."""
    ana, beto, carla = [await make_user(session, n) for n in ("Ana", "Beto", "Carla")]
    zone = await make_zone(session)
    esperanza = await make_field(session, "La Esperanza", zones=[(zone, "1")])
    unmapped = await make_field(session, "Sin zonas")
    await add_member(session, ana, esperanza)
    await add_member(session, beto, esperanza, FieldRole.AGRONOMIST)
    await add_member(session, ana, unmapped)
    await session.commit()
    return World(ana=ana, beto=beto, carla=carla, field=esperanza, unmapped=unmapped)


async def _create(client: AsyncClient, user: User, **body: object) -> Response:
    return await client.post("/api/v1/alerts", json=body, headers=auth(user))


async def test_create_returns_201_and_location(client: AsyncClient, world: World) -> None:
    ana, field = world.ana, world.field
    response = await _create(client, ana, field_id=field.id, event_type="frost", threshold=70)
    assert response.status_code == 201
    body = response.json()
    assert response.headers["Location"] == f"/api/v1/alerts/{body['id']}"
    assert body["field_id"] == field.id
    assert body["event_type"] == "frost"
    assert body["threshold"] == 70
    assert body["is_active"] is True
    assert body["zones_mapped"] is True
    assert set(body) == {
        "id",
        "field_id",
        "event_type",
        "threshold",
        "is_active",
        "zones_mapped",
        "created_at",
        "updated_at",
    }


async def test_zones_mapped_false_on_an_unmapped_field(client: AsyncClient, world: World) -> None:
    response = await _create(
        client,
        world.ana,
        field_id=world.unmapped.id,
        event_type="rain",
        threshold=50,
        is_active=False,
    )
    assert response.status_code == 201
    assert response.json()["zones_mapped"] is False
    assert response.json()["is_active"] is False


async def test_get_list_patch_delete(client: AsyncClient, world: World) -> None:
    ana, field = world.ana, world.field
    created = await _create(client, ana, field_id=field.id, event_type="frost", threshold=70)
    alert_id = created.json()["id"]

    got = await client.get(f"/api/v1/alerts/{alert_id}", headers=auth(ana))
    assert got.status_code == 200 and got.json() == created.json()

    listed = await client.get("/api/v1/alerts", headers=auth(ana))
    assert listed.status_code == 200
    assert listed.json() == {"items": [created.json()], "limit": 50, "offset": 0}

    patched = await client.patch(
        f"/api/v1/alerts/{alert_id}", json={"threshold": 80, "is_active": False}, headers=auth(ana)
    )
    assert patched.status_code == 200
    assert patched.json()["threshold"] == 80 and patched.json()["is_active"] is False
    assert datetime.fromisoformat(patched.json()["updated_at"]) > datetime.fromisoformat(
        created.json()["updated_at"]
    )
    again = await client.get(f"/api/v1/alerts/{alert_id}", headers=auth(ana))
    assert again.json() == patched.json()

    deleted = await client.delete(f"/api/v1/alerts/{alert_id}", headers=auth(ana))
    assert deleted.status_code == 204 and deleted.content == b""
    missing = await client.get(f"/api/v1/alerts/{alert_id}", headers=auth(ana))
    assert missing.status_code == 404


async def test_list_filters_and_pagination(client: AsyncClient, world: World) -> None:
    ana, field, unmapped = world.ana, world.field, world.unmapped
    ids = []
    for field_id, code, active in [
        (field.id, "frost", True),
        (field.id, "rain", False),
        (unmapped.id, "frost", True),
    ]:
        r = await _create(
            client, ana, field_id=field_id, event_type=code, threshold=60, is_active=active
        )
        ids.append(r.json()["id"])

    async def listed(**params: str | int | bool) -> list[int]:
        r = await client.get("/api/v1/alerts", params=params, headers=auth(ana))
        assert r.status_code == 200
        return [a["id"] for a in r.json()["items"]]

    assert await listed() == ids
    assert await listed(field_id=field.id) == ids[:2]
    assert await listed(event_type="frost") == [ids[0], ids[2]]
    assert await listed(is_active=False) == [ids[1]]
    assert await listed(field_id=field.id, event_type="frost", is_active=True) == [ids[0]]
    assert await listed(limit=2) == ids[:2]
    assert await listed(limit=2, offset=2) == ids[2:]
    page = await client.get("/api/v1/alerts", params={"limit": 1, "offset": 1}, headers=auth(ana))
    assert (page.json()["limit"], page.json()["offset"]) == (1, 1)


@pytest.mark.parametrize("header", [None, "abc", "-1", "1.5", "999999"])
async def test_401_without_a_known_identity(
    client: AsyncClient, world: World, header: str | None
) -> None:
    headers = {"X-User-Id": header} if header is not None else {}
    for method, path in [
        ("GET", "/api/v1/alerts"),
        ("GET", "/api/v1/fields"),
        ("GET", "/api/v1/event-types"),
        ("POST", "/api/v1/alerts"),
    ]:
        response = await client.request(method, path, headers=headers, json={})
        assert response.status_code == 401, (method, path)
        assert response.json() == {"detail": "missing or unknown X-User-Id"}


async def test_404_creating_on_a_field_the_caller_is_not_a_member_of(
    client: AsyncClient,
    world: World,
) -> None:
    r = await _create(
        client, world.carla, field_id=world.field.id, event_type="frost", threshold=70
    )
    assert r.status_code == 404 and r.json() == {"detail": "field not found"}
    r = await _create(client, world.ana, field_id=999999, event_type="frost", threshold=70)
    assert r.status_code == 404


async def test_another_members_alert_is_not_found(client: AsyncClient, world: World) -> None:
    ana, beto, field = world.ana, world.beto, world.field
    alert_id = (
        await _create(client, ana, field_id=field.id, event_type="frost", threshold=70)
    ).json()["id"]
    path = f"/api/v1/alerts/{alert_id}"
    assert (await client.get(path, headers=auth(beto))).status_code == 404
    assert (await client.patch(path, json={"threshold": 10}, headers=auth(beto))).status_code == 404
    assert (await client.delete(path, headers=auth(beto))).status_code == 404
    assert (await client.get("/api/v1/alerts", headers=auth(beto))).json()["items"] == []
    still = await client.get(path, headers=auth(ana))
    assert still.status_code == 200 and still.json()["threshold"] == 70


async def test_two_members_can_each_have_the_same_alert(client: AsyncClient, world: World) -> None:
    for user in (world.ana, world.beto):
        r = await _create(client, user, field_id=world.field.id, event_type="heat", threshold=60)
        assert r.status_code == 201


async def test_409_duplicate_for_the_same_user(client: AsyncClient, world: World) -> None:
    body = {"field_id": world.field.id, "event_type": "frost", "threshold": 70}
    assert (await _create(client, world.ana, **body)).status_code == 201
    r = await _create(client, world.ana, **{**body, "threshold": 40})
    assert r.status_code == 409
    assert "already" in r.json()["detail"]


async def test_404_when_the_membership_disappears_before_the_insert(
    client: AsyncClient,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def passes_check(*_: object) -> bool:
        return True  # as if the membership was removed right after the check

    monkeypatch.setattr(alerts_service, "is_member", passes_check)
    r = await _create(
        client, world.carla, field_id=world.field.id, event_type="frost", threshold=70
    )
    assert r.status_code == 404 and r.json() == {"detail": "field not found"}


@pytest.mark.parametrize(
    "body",
    [
        {"threshold": 0},
        {"threshold": 101},
        {"threshold": "70"},
        {"threshold": 70.5},
        {"event_type": "hail"},
        {"event_type": ""},
        {"field_id": "1"},
        {"is_active": "yes"},
    ],
)
async def test_422_on_invalid_create(
    client: AsyncClient, world: World, body: dict[str, object]
) -> None:
    base = {"field_id": world.field.id, "event_type": "frost", "threshold": 70}
    r = await _create(client, world.ana, **{**base, **body})
    assert r.status_code == 422
    assert "detail" in r.json()


async def test_422_unknown_event_type_is_reported_by_code(
    client: AsyncClient, world: World
) -> None:
    r = await _create(client, world.ana, field_id=world.field.id, event_type="hail", threshold=70)
    assert r.json() == {"detail": "unknown event type: 'hail'"}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"field_id": 2},
        {"event_type": "rain"},
        {"threshold": None},
        {"is_active": None},
        {"threshold": 0},
        {"threshold": 101},
        {"threshold": 50, "extra": 1},
    ],
)
async def test_422_on_invalid_patch(
    client: AsyncClient, world: World, body: dict[str, object]
) -> None:
    alert_id = (
        await _create(client, world.ana, field_id=world.field.id, event_type="frost", threshold=70)
    ).json()["id"]
    r = await client.patch(f"/api/v1/alerts/{alert_id}", json=body, headers=auth(world.ana))
    assert r.status_code == 422


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"event_type": "hail"}, {"is_active": "maybe"}],
)
async def test_422_on_invalid_list_query(
    client: AsyncClient, world: World, params: dict[str, str | int]
) -> None:
    r = await client.get("/api/v1/alerts", params=params, headers=auth(world.ana))
    assert r.status_code == 422


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_503_while_a_dispatcher_holds_the_alert(
    client: AsyncClient,
    world: World,
    engine: AsyncEngine,
    method: str,
) -> None:
    alert_id = (
        await _create(client, world.ana, field_id=world.field.id, event_type="frost", threshold=70)
    ).json()["id"]
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT id FROM alerts WHERE id = :id FOR NO KEY UPDATE"), {"id": alert_id}
        )
        started = time.monotonic()
        r = await client.request(
            method,
            f"/api/v1/alerts/{alert_id}",
            json={"threshold": 80},
            headers=auth(world.ana),
        )
        elapsed = time.monotonic() - started
        await holder.rollback()
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "10"
    assert 4 <= elapsed < 8  # the connection's 5-second lock timeout
    after = await client.get(f"/api/v1/alerts/{alert_id}", headers=auth(world.ana))
    assert after.json()["threshold"] == 70


async def test_delete_removes_the_alerts_notifications(
    client: AsyncClient,
    world: World,
    session: AsyncSession,
) -> None:
    ana, field = world.ana, world.field
    alert = await make_alert(session, ana, field)
    await make_notification(
        session,
        alert,
        datetime.now().date(),  # noqa: DTZ005
        next_attempt_at=datetime.fromisoformat("2026-11-13T00:00:00+00:00"),
    )
    await session.commit()
    r = await client.delete(f"/api/v1/alerts/{alert.id}", headers=auth(ana))
    assert r.status_code == 204
    assert (await session.execute(select(Notification))).first() is None
    assert (await session.execute(select(Alert))).first() is None


async def test_unexpected_error_is_500_logged_once_without_internals(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    async def explode() -> User:
        raise RuntimeError("secret internals")

    client.app.dependency_overrides[get_current_user] = explode  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR):
        r = await client.get("/api/v1/event-types")
    assert r.status_code == 500
    assert r.json() == {"detail": "internal server error"}
    errors = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert [rec.message for rec in errors] == ["unhandled_error"]
    assert errors[0].path == "/api/v1/event-types"  # type: ignore[attr-defined]


async def test_unexpected_database_error_is_500(
    client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import DBAPIError

    async def broken(*_: object, **__: object) -> None:
        raise DBAPIError("SELECT", {}, Exception("connection reset"))

    monkeypatch.setattr(alerts_service, "list_alerts", broken)
    r = await client.get("/api/v1/alerts", headers=auth(world.ana))
    assert r.status_code == 500


async def test_concurrent_duplicate_creates_give_one_201_and_one_409(
    client: AsyncClient,
    world: World,
) -> None:
    body = {"field_id": world.field.id, "event_type": "frost", "threshold": 70}
    results = await asyncio.gather(*(_create(client, world.ana, **body) for _ in range(2)))
    assert sorted(r.status_code for r in results) == [201, 409]
