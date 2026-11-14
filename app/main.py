"""Application factory for the HTTP API.

Run with ``uvicorn --factory app.main:create_app``. The engine is built when the app is created,
not in the lifespan, so test clients that skip the lifespan still get a working app; the
lifespan only disposes the engine on shutdown.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import health
from app.config import Settings, get_settings
from app.db import build_engine, build_session_factory
from app.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API with its own engine and session factory, stored on ``app.state``."""
    settings = settings or get_settings()
    configure_logging(settings)
    engine = build_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(title="Weather alerts", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)

    app.include_router(health.router)
    return app
