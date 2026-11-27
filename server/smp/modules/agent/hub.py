# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""In-memory coordination for agents (SMP runs one server process).

AgentHub: heartbeats long-poll. An agent whose config hasn't changed waits here
until something that could change it happens (a policy, setting or device changed,
a teacher started watching a room, ...) or the wait times out. `generation` counts
changes so a change between "computed the config" and "started waiting" isn't lost.

ReplayGuard: remembers recently seen request signatures so a captured request
can't be sent again within the allowed clock skew.
"""

from __future__ import annotations

import asyncio
import time


class AgentHub:
    def __init__(self) -> None:
        self.generation = 0
        self._changed = asyncio.Event()

    def notify(self) -> None:
        """Something agents' configs depend on changed: wake every waiting heartbeat.
        (Coarse on purpose: recomputing a config is a couple of small queries.)"""
        self.generation += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait(self, since_generation: int, max_wait: float) -> bool:
        """Wait until the generation moves past `since_generation`. True if it did."""
        if self.generation != since_generation:
            return True
        changed = self._changed
        try:
            await asyncio.wait_for(changed.wait(), max_wait)
            return True
        except TimeoutError:
            return False


class ReplayGuard:
    def __init__(self, ttl_seconds: float, clock=time.monotonic) -> None:
        self.ttl = ttl_seconds
        self._clock = clock
        self._seen: dict[str, float] = {}

    def first_time(self, signature: str) -> bool:
        """True (and remembered) if this signature wasn't seen within the TTL."""
        now = self._clock()
        if len(self._seen) > 10_000:
            self._seen = {s: t for s, t in self._seen.items() if t > now}
        expires = self._seen.get(signature)
        if expires is not None and expires > now:
            return False
        self._seen[signature] = now + self.ttl
        return True
