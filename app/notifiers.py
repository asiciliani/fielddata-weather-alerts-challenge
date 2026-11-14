"""The notifier port: how a grouped message reaches the user.

The only implementation writes the message to the log, standing in for WhatsApp. A real client
would live behind the same port; a blocking SDK would run in ``asyncio.to_thread`` there.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboundMessage:
    """One message for one alert, covering one or more notification rows."""

    alert_id: int
    user_id: int
    notification_ids: tuple[int, ...]
    to: str  # phone number in E.164; never logged
    text: str


class NotifierError(Exception):
    """A failed send, described only by a kind and a code.

    The provider's raw error text can contain the recipient's phone number, so it is never
    stored or logged: a real client maps provider errors to these two fields.
    """

    def __init__(self, kind: str, code: str) -> None:
        super().__init__(f"{kind}: {code}")
        self.kind = kind
        self.code = code


class Notifier(Protocol):
    async def send(self, message: OutboundMessage) -> None:
        """Deliver the message or raise. Must be safe to call concurrently."""
        ...


class LogNotifier:
    """Writes each message to the log instead of sending it."""

    async def send(self, message: OutboundMessage) -> None:
        logger.info(
            "message_delivered",
            extra={
                "alert_id": message.alert_id,
                "user_id": message.user_id,
                "notification_ids": list(message.notification_ids),
                "text": message.text,
            },
        )
