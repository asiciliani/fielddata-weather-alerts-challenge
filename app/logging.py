"""Logging setup: JSON lines for production, a readable format for local use.

Messages are static event names (``evaluation_completed``); identifiers and counts go in
``extra`` and come out as top-level JSON fields. Phone numbers are never logged.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

# Attributes every LogRecord has; anything else on a record came from ``extra``.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # Uvicorn's ANSI-coloured copy of the message
}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _RECORD_ATTRS}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, then the ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable lines for local use: the message followed by ``key=value`` extras."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = " ".join(f"{k}={v}" for k, v in _extras(record).items())
        return f"{line} {extras}" if extras else line


class _AppHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marks the handler this module installs, so reconfiguring replaces only it."""


def configure_logging(settings: Settings) -> None:
    """Send every log record to standard output in the configured format.

    Safe to call more than once: it replaces only the handler it installed before, and leaves
    other handlers (for example pytest's capture handler) alone.
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, _AppHandler)]:
        root.removeHandler(handler)
    handler = _AppHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else ConsoleFormatter())
    root.addHandler(handler)
    root.setLevel(settings.log_level)
    # Uvicorn installs its own handlers; route its records through ours so every line has one
    # format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
