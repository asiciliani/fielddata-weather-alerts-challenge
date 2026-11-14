from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import FieldRole
from tests.factories import add_member, make_field, make_user


async def test_fields_lists_only_the_callers_fields_with_role(
    client: AsyncClient, session: AsyncSession
) -> None:
    ana, beto = await make_user(session, "Ana"), await make_user(session, "Beto")
    shared = await make_field(session, "La Compartida")
    own = await make_field(session, "La Esperanza")
    await make_field(session, "Ajeno")
    await add_member(session, ana, shared, FieldRole.OWNER)
    await add_member(session, beto, shared, FieldRole.AGRONOMIST)
    await add_member(session, ana, own, FieldRole.MANAGER)
    await session.commit()

    r = await client.get("/api/v1/fields", headers={"X-User-Id": str(ana.id)})
    assert r.status_code == 200
    assert r.json() == {
        "items": [
            {"id": shared.id, "name": "La Compartida", "role": "owner"},
            {"id": own.id, "name": "La Esperanza", "role": "manager"},
        ],
        "limit": 50,
        "offset": 0,
    }
    r = await client.get("/api/v1/fields", headers={"X-User-Id": str(beto.id)})
    assert r.json()["items"] == [{"id": shared.id, "name": "La Compartida", "role": "agronomist"}]
    r = await client.get(
        "/api/v1/fields", params={"limit": 1, "offset": 1}, headers={"X-User-Id": str(ana.id)}
    )
    assert [f["name"] for f in r.json()["items"]] == ["La Esperanza"]
    r = await client.get("/api/v1/fields", params={"limit": 0}, headers={"X-User-Id": str(ana.id)})
    assert r.status_code == 422


async def test_event_types_lists_the_four_types(client: AsyncClient, session: AsyncSession) -> None:
    user = await make_user(session)
    await session.commit()
    r = await client.get("/api/v1/event-types", headers={"X-User-Id": str(user.id)})
    assert r.status_code == 200
    assert r.json() == [
        {"code": "frost", "name": "helada"},
        {"code": "rain", "name": "lluvia"},
        {"code": "strong_wind", "name": "viento fuerte"},
        {"code": "heat", "name": "calor extremo"},
    ]
