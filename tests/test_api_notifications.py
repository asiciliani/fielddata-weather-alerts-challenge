from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import FieldRole, NotificationStatus
from app.models import Notification
from tests.factories import add_member, make_alert, make_field, make_notification, make_user

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)


def day(offset: int) -> date:
    return date(2026, 11, 13) + timedelta(days=offset)


@pytest.fixture
async def world(session: AsyncSession) -> dict[str, Any]:
    """Ana: frost alert on La Compartida (3 rows) and rain on La Esperanza (1 row). Beto, also
    a member of La Compartida, has his own frost alert there (1 row)."""
    ana, beto = await make_user(session, "Ana"), await make_user(session, "Beto")
    shared, own = await make_field(session, "La Compartida"), await make_field(session, "Otra")
    await add_member(session, ana, shared)
    await add_member(session, beto, shared, FieldRole.AGRONOMIST)
    await add_member(session, ana, own)
    frost = await make_alert(session, ana, shared, "frost", 70)
    rain = await make_alert(session, ana, own, "rain", 60)
    betos = await make_alert(session, beto, shared, "frost", 50)
    n1 = await make_notification(
        session, frost, day(1), next_attempt_at=NOW, status=NotificationStatus.SENT, sent_at=NOW
    )
    n2 = await make_notification(session, frost, day(2), next_attempt_at=NOW)
    n3 = await make_notification(
        session, frost, day(-1), next_attempt_at=NOW, status=NotificationStatus.FAILED
    )
    n4 = await make_notification(
        session, rain, day(1), next_attempt_at=NOW, affected_area="0.4", probability=65
    )
    await make_notification(session, betos, day(1), next_attempt_at=NOW)
    await session.commit()
    return {
        "ana": ana,
        "beto": beto,
        "shared": shared,
        "own": own,
        "frost": frost,
        "rain": rain,
        "ids": [n1.id, n2.id, n3.id, n4.id],
    }


async def _list(client: AsyncClient, user_id: int, **params: Any) -> Any:
    r = await client.get(
        "/api/v1/notifications", params=params, headers={"X-User-Id": str(user_id)}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_only_the_callers_notifications_newest_first(
    client: AsyncClient, world: dict[str, Any]
) -> None:
    body = await _list(client, world["ana"].id)
    assert [n["id"] for n in body["items"]] == sorted(world["ids"], reverse=True)
    assert (body["limit"], body["offset"]) == (50, 0)
    beto = await _list(client, world["beto"].id)
    assert len(beto["items"]) == 1 and beto["items"][0]["id"] not in world["ids"]


async def test_notification_shape(client: AsyncClient, world: dict[str, Any]) -> None:
    [rain] = (await _list(client, world["ana"].id, alert_id=world["rain"].id))["items"]
    assert rain == {
        "id": world["ids"][3],
        "alert_id": world["rain"].id,
        "forecast_id": None,
        "field_id": world["own"].id,
        "event_type": "rain",
        "forecast_date": day(1).isoformat(),
        "threshold": 60,
        "probability": 65,
        "affected_area": "0.4000",
        "status": "pending",
        "sent_at": None,
        "created_at": rain["created_at"],
    }


async def test_filters(client: AsyncClient, world: dict[str, Any]) -> None:
    ana, ids = world["ana"].id, world["ids"]

    async def ids_for(**params: Any) -> list[int]:
        return [n["id"] for n in (await _list(client, ana, **params))["items"]]

    assert await ids_for(status="sent") == [ids[0]]
    assert await ids_for(status="failed") == [ids[2]]
    assert await ids_for(status="pending") == [ids[3], ids[1]]
    assert await ids_for(field_id=world["shared"].id) == [ids[2], ids[1], ids[0]]
    assert await ids_for(alert_id=world["rain"].id) == [ids[3]]
    assert await ids_for(field_id=world["shared"].id, status="pending") == [ids[1]]


async def test_pagination(client: AsyncClient, world: dict[str, Any]) -> None:
    ids = sorted(world["ids"], reverse=True)
    page = await _list(client, world["ana"].id, limit=2, offset=1)
    assert [n["id"] for n in page["items"]] == ids[1:3]
    assert (page["limit"], page["offset"]) == (2, 1)


async def test_401_and_422(client: AsyncClient, world: dict[str, Any]) -> None:
    assert (await client.get("/api/v1/notifications")).status_code == 401
    headers = {"X-User-Id": str(world["ana"].id)}
    for params in ({"status": "lost"}, {"limit": 0}, {"limit": 101}, {"alert_id": "x"}):
        r = await client.get("/api/v1/notifications", params=params, headers=headers)
        assert r.status_code == 422, params


async def test_forecast_id_is_returned_when_linked(
    client: AsyncClient, world: dict[str, Any], session: AsyncSession
) -> None:
    from tests.factories import make_forecast, make_zone

    zone = await make_zone(session)
    forecast = await make_forecast(session, zone, "frost", day(5), 90)
    linked: Notification = await make_notification(
        session, world["frost"], day(5), next_attempt_at=NOW, forecast=forecast
    )
    await session.commit()
    [item] = [n for n in (await _list(client, world["ana"].id))["items"] if n["id"] == linked.id]
    assert item["forecast_id"] == forecast.id
