"""The caller's fields."""

import sqlalchemy as sa
from fastapi import APIRouter

from app.api.deps import CurrentUser, PaginationDep, SessionDep
from app.models import Field, FieldMember
from app.schemas import FieldRead, Page

router = APIRouter(prefix="/fields", tags=["fields"])


@router.get("")
async def list_fields(
    session: SessionDep, user: CurrentUser, page: PaginationDep
) -> Page[FieldRead]:
    """Fields the caller is a member of, with the caller's role."""
    rows = await session.execute(
        sa.select(Field.id, Field.name, FieldMember.role)
        .join(FieldMember, FieldMember.field_id == Field.id)
        .where(FieldMember.user_id == user.id)
        .order_by(Field.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    items = [FieldRead(id=id_, name=name, role=role) for id_, name, role in rows.all()]
    return Page(items=items, limit=page.limit, offset=page.offset)
