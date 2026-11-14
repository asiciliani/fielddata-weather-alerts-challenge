"""Domain errors and how the API answers them.

Expected client errors (401, 404, 409, 422, 503) are answered without logging. Anything else is
logged once at ERROR and answered 500 with no internals in the body.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

LOCK_NOT_AVAILABLE = "55P03"  # PostgreSQL SQLSTATE raised when lock_timeout expires
BUSY_RETRY_AFTER_SECONDS = 10


class DomainError(Exception):
    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Unauthenticated(DomainError):
    status_code = 401

    def __init__(self) -> None:
        super().__init__("missing or unknown X-User-Id")


class NotFound(DomainError):
    status_code = 404

    def __init__(self, resource: str) -> None:
        super().__init__(f"{resource} not found")


class AlertAlreadyExists(DomainError):
    status_code = 409

    def __init__(self) -> None:
        super().__init__("you already have an alert for this field and event type")


class UnknownEventType(DomainError):
    status_code = 422

    def __init__(self, code: str) -> None:
        super().__init__(f"unknown event type: {code!r}")


def is_lock_timeout(exc: DBAPIError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE


async def _domain_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    headers = {"WWW-Authenticate": "X-User-Id"} if isinstance(exc, Unauthenticated) else None
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)


async def _database_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DBAPIError)
    if not is_lock_timeout(exc):
        raise exc  # unexpected: the catch-all middleware logs it and answers 500
    # The alert is locked: a dispatcher is sending it, or an evaluation is inserting for it.
    return JSONResponse(
        {"detail": "the alert is busy being processed; retry shortly"},
        status_code=503,
        headers={"Retry-After": str(BUSY_RETRY_AFTER_SECONDS)},
    )


class CatchUnhandledErrors:
    """Logs an unexpected exception once and answers 500.

    A plain exception handler for ``Exception`` would answer 500 too, but Starlette re-raises the
    exception afterwards and the server logs it a second time; this middleware stops it here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            logger.exception(
                "unhandled_error", extra={"method": scope["method"], "path": scope["path"]}
            )
            if started:
                return  # the response is already on its way; nothing else can be sent
            response = JSONResponse({"detail": "internal server error"}, status_code=500)
            await response(scope, receive, send)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error)
    app.add_exception_handler(DBAPIError, _database_error)
    app.add_middleware(CatchUnhandledErrors)
