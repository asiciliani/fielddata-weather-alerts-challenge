"""Database engine and session factory builders."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings

# Set on every connection: a stuck lock or a runaway query fails instead of hanging.
_COMMON_SERVER_SETTINGS = {
    "lock_timeout": "5s",
    "statement_timeout": "30s",
}

# Worker connections hold a transaction open while a batch is being sent. If the worker dies
# without closing its connection, PostgreSQL ends that transaction (and frees the batch's locks)
# after the idle timeout, and the keepalives detect a dead peer. asyncpg has no client-side
# keepalive options, so they are set as server-side session settings.
_WORKER_SERVER_SETTINGS = {
    "idle_in_transaction_session_timeout": "30s",
    "tcp_keepalives_idle": "10",
    "tcp_keepalives_interval": "5",
    "tcp_keepalives_count": "3",
}


def build_engine(settings: Settings, *, worker: bool = False) -> AsyncEngine:
    """An async engine whose connections carry this system's session timeouts."""
    server_settings = dict(_COMMON_SERVER_SETTINGS)
    if worker:
        server_settings |= _WORKER_SERVER_SETTINGS
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        connect_args={"server_settings": server_settings},
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions whose objects stay readable after commit (no implicit reload on access)."""
    return async_sessionmaker(engine, expire_on_commit=False)
