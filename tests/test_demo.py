from datetime import timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import database_now
from app.models import Notification, User
from app.seed import seed
from app.services.evaluation import evaluate_alerts
from tests.factories import add_member, make_field, make_forecast, make_user, make_zone


async def _today(session: AsyncSession, client: AsyncClient):  # type: ignore[no-untyped-def]
    settings = client.app.state.settings  # type: ignore[attr-defined]
    return (await database_now(session)).astimezone(settings.tz).date()


async def test_demo_page_and_its_data_file_are_served(client: AsyncClient) -> None:
    page = await client.get("/demo")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Qué campos reciben alerta" in page.text
    data = await client.get("/demo/static/field-outlines.json")
    assert data.status_code == 200
    assert set(data.json()["fields"]) == {
        "La Esperanza",
        "Los Álamos",
        "El Rincón",
        "La Compartida",
    }


async def test_outlook_returns_zones_and_forecasts_of_the_window_only(
    client: AsyncClient, session: AsyncSession
) -> None:
    today = await _today(session, client)
    user = await make_user(session)
    zone_a, zone_b, other = [await make_zone(session) for _ in range(3)]
    field = await make_field(session, "Los Álamos", zones=[(zone_a, "0.6"), (zone_b, "0.4")])
    await add_member(session, user, field)
    for offset in (-1, 0, 7, 8):
        await make_forecast(session, zone_a, "frost", today + timedelta(days=offset), 50)
    await make_forecast(session, other, "frost", today, 99)
    await session.commit()

    r = await client.get(f"/api/v1/fields/{field.id}/outlook", headers={"X-User-Id": str(user.id)})
    assert r.status_code == 200
    body = r.json()
    assert (body["name"], body["role"], body["today"]) == ("Los Álamos", "owner", today.isoformat())
    assert body["end_date"] == (today + timedelta(days=7)).isoformat()
    assert Decimal(body["min_affected_area"]) == Decimal("0.10")
    assert {(z["id"], Decimal(z["coverage"])) for z in body["zones"]} == {
        (zone_a.id, Decimal("0.6")),
        (zone_b.id, Decimal("0.4")),
    }
    assert [f["forecast_date"] for f in body["forecasts"]] == [
        today.isoformat(),
        (today + timedelta(days=7)).isoformat(),
    ]
    assert body["alerts"] == []


async def test_outlook_is_404_for_a_non_member(client: AsyncClient, session: AsyncSession) -> None:
    owner, stranger = await make_user(session, "Ana"), await make_user(session, "Carla")
    field = await make_field(session)
    await add_member(session, owner, field)
    await session.commit()
    r = await client.get(
        f"/api/v1/fields/{field.id}/outlook", headers={"X-User-Id": str(stranger.id)}
    )
    assert r.status_code == 404
    r = await client.get("/api/v1/fields/999999/outlook", headers={"X-User-Id": str(owner.id)})
    assert r.status_code == 404


async def test_outlook_firing_dates_match_what_evaluation_creates(
    client: AsyncClient, session: AsyncSession
) -> None:
    today = await _today(session, client)
    await seed(session, today=today, days=14, rng_seed=42)
    await session.commit()

    fired: set[tuple[int, str, Decimal, int]] = set()
    slivers: list[tuple[int, str]] = []
    for user in (await session.execute(select(User))).scalars().all():
        headers = {"X-User-Id": str(user.id)}
        for item in (await client.get("/api/v1/fields", headers=headers)).json()["items"]:
            outlook = (
                await client.get(f"/api/v1/fields/{item['id']}/outlook", headers=headers)
            ).json()
            for alert in outlook["alerts"]:
                for d in alert["dates"]:
                    key = (alert["id"], d["forecast_date"])
                    if d["fires"]:
                        fired.add((*key, Decimal(d["affected_area"]), d["probability"]))
                    else:
                        slivers.append(key)

    await evaluate_alerts(session, today=today, horizon_days=7, min_affected_area=Decimal("0.10"))
    created = {
        (n.alert_id, n.forecast_date.isoformat(), n.affected_area, n.probability)
        for n in (await session.execute(select(Notification))).scalars().all()
    }
    assert fired == created and len(created) == 4
    assert len(slivers) == 1  # El Rincón's 5% sliver reaches the threshold but does not fire
