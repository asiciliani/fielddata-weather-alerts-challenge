"""FastAPI dependencies shared by the routes."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import Unauthenticated
from app.models import User


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request; anything not committed by the route is rolled back."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    session: SessionDep,
    x_user_id: Annotated[str | None, Header(description="stand-in for real authentication")] = None,
) -> User:
    """The caller, from the ``X-User-Id`` header. Real authentication replaces this function."""
    if x_user_id is None or not x_user_id.strip().isdecimal():
        raise Unauthenticated()
    user = await session.get(User, int(x_user_id))
    if user is None:
        raise Unauthenticated()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


@dataclass(frozen=True)
class Pagination:
    limit: int
    offset: int


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


PaginationDep = Annotated[Pagination, Depends(get_pagination)]
