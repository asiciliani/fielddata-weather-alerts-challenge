"""A notifier for tests: records messages and can fail, hang or wait on an event."""

import asyncio
from collections.abc import Callable

from app.notifiers import NotifierError, OutboundMessage


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self.attempted: list[OutboundMessage] = []
        self.fail_with: BaseException | Callable[[OutboundMessage], BaseException | None] | None
        self.fail_with = None
        self.hang = False
        self.gate: asyncio.Event | None = None  # when set, sends wait for it
        self.entered = asyncio.Event()  # set once a send has started

    async def send(self, message: OutboundMessage) -> None:
        self.attempted.append(message)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.hang:
            await asyncio.Event().wait()
        error = self.fail_with(message) if callable(self.fail_with) else self.fail_with
        if error is not None:
            raise error
        self.sent.append(message)


def channel_down() -> NotifierError:
    return NotifierError("unavailable", "503")
