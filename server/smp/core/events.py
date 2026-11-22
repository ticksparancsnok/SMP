# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Event bus.

Subjects use NATS syntax so the in-process bus can be swapped for NATS (or similar)
once modules run as separate processes:
    "core.device.updated"   exact
    "core.device.*"         one token wildcard
    "core.>"                matches everything below core.

Events are queued on the Ctx and only published after the DB transaction commits,
so subscribers never see changes that were rolled back.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from smp.core.db import utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    subject: str
    org_id: uuid.UUID
    payload: dict[str, Any]
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    at: datetime = field(default_factory=utcnow)


Handler = Callable[[Event], Awaitable[None]]


def subject_matches(pattern: str, subject: str) -> bool:
    p, s = pattern.split("."), subject.split(".")
    for i, tok in enumerate(p):
        if tok == ">":
            return len(s) > i
        if i >= len(s) or (tok != "*" and tok != s[i]):
            return False
    return len(p) == len(s)


class EventBus(Protocol):
    def subscribe(self, pattern: str, handler: Handler) -> None: ...
    async def publish(self, event: Event) -> None: ...


class InProcessEventBus:
    """Handlers run as background tasks; a failing handler is logged and never
    affects the publisher or other handlers."""

    def __init__(self) -> None:
        self._subs: list[tuple[str, Handler]] = []
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs.append((pattern, handler))

    async def publish(self, event: Event) -> None:
        for pattern, handler in self._subs:
            if subject_matches(pattern, event.subject):
                task = asyncio.create_task(self._run(handler, event))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _run(handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            log.exception("event handler %r failed for %s", handler, event.subject)

    async def drain(self) -> None:
        """Wait for in-flight handlers (used on shutdown and in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
