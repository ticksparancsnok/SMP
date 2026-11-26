# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Login throttling.

Two independent limits, both checked before any password hashing:

  * per account: `max_failures` failed logins for one username (from any IP) within
    FAILURE_MEMORY_SECONDS lock that username for `lockout_seconds`. Every place that
    checks a password counts here: wrong current passwords in the password change too
    (an unlocked teacher PC is no way around it; reaching the limit there also ends the
    account's sessions). While locked,
    every attempt is refused, even with the right password, so rotating source IPs
    (an IPv6 /64 is 2^64 addresses) gains an attacker nothing. Each lockout starts a
    fresh count;
  * per IP: more than `ip_per_minute` attempts per minute from one address are
    refused (caps the CPU/RAM argon2 costs, and spraying many usernames).

Both apply identically to existing and unknown usernames, so neither is an
enumeration oracle. The price of the per-account limit: anyone can keep an account
(e.g. the admin's) locked by failing 5 times a minute. The lockout is short (60 s by
default) to keep that a nuisance rather than a denial of service.

TODO(evaluate, issue #2): full user lockouts (account stays locked until an admin unlocks it)
and, after repeated lockouts, resetting the password to a new one-time password that
the class teacher/admin hands out. Both stop slow guessing completely but make
lockout-DoS against staff accounts much worse; decide with the throttle's audit data
(core.auth.login_failed per username) at hand.

State is in memory, per server process (SMP runs one process). It survives restarts
because every counted attempt is also in the audit log, and the server re-seeds the
throttle from it on startup (see seed_throttle_from_audit). Behind a reverse proxy
the client IP must be the real one (uvicorn --forwarded-allow-ips), otherwise
everybody shares the proxy's IP.
"""

from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio.session import AsyncSession

# Failures older than this no longer count towards a lockout.
FAILURE_MEMORY_SECONDS = 15 * 60


class LoginThrottle:  # ruff: ignore[undocumented-public-class]
    __slots__ = (
        "_attempts",
        "_calls",
        "_clock",
        "_failures",
        "_locked_until",
        "ip_per_minute",
        "lockout",
        "max_failures"
    )

    def __init__(  # ruff: ignore[undocumented-public-init]
        self,
        max_failures: int,
        lockout_seconds: float,
        ip_per_minute: int,
        clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.max_failures = max_failures
        self.lockout = lockout_seconds
        self.ip_per_minute = ip_per_minute
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}  # username -> failure times  # ruff: ignore[line-too-long]
        self._locked_until: dict[str, float] = {}  # username -> end of lockout
        self._attempts: dict[str, deque[float]] = {}  # ip -> attempt times
        self._calls = 0

    @staticmethod
    def _key(username: str) -> str:
        return username.strip().casefold()

    @staticmethod
    def _prune(q: deque[float], older_than: float) -> None:
        while q and q[0] <= older_than:
            q.popleft()

    def _sweep(self, now: float) -> None:
        """Drop stale entries now and then so memory stays bounded."""
        self._calls += 1
        if self._calls % 1000:
            return
        for store, horizon in (
            (self._failures, FAILURE_MEMORY_SECONDS),
            (self._attempts, 60)
        ):
            for k in list(store):
                self._prune(store[k], now - horizon)
                if not store[k]:
                    del store[k]
        for k, until in list(self._locked_until.items()):
            if until <= now:
                del self._locked_until[k]

    def locked(self, username: str) -> bool:  # ruff: ignore[undocumented-public-method]
        until = self._locked_until.get(self._key(username))
        return (until is not None) and (until > self._clock())

    def allowed(self, ip: str, username: str) -> bool:  # ruff: ignore[undocumented-public-method]
        now = self._clock()
        self._sweep(now)
        attempts = self._attempts.get(ip)
        if attempts is not None:
            self._prune(attempts, now - 60.0)
            if len(attempts) >= self.ip_per_minute:
                return False
        return not self.locked(username)

    def record_attempt(self, ip: str, seconds_ago: float = 0.0) -> None:  # ruff: ignore[undocumented-public-method]
        self._attempts.setdefault(ip, deque()).append(self._clock() - seconds_ago)  # ruff: ignore[line-too-long]

    def record_failure(self, username: str, seconds_ago: float = 0.0) -> None:  # ruff: ignore[undocumented-public-method]
        at = self._clock() - seconds_ago
        key = self._key(username)
        q = self._failures.setdefault(key, deque())
        self._prune(q, at - FAILURE_MEMORY_SECONDS)
        q.append(at)
        if len(q) >= self.max_failures:
            self._locked_until[key] = at + self.lockout
            q.clear()  # a new count starts after the lockout

    def record_success(self, username: str) -> None:  # ruff: ignore[undocumented-public-method]
        self._failures.pop(self._key(username), None)

    def reset(self) -> None:  # ruff: ignore[undocumented-public-method]
        self._failures.clear()
        self._locked_until.clear()
        self._attempts.clear()


async def seed_throttle_from_audit(throttle: LoginThrottle, db: AsyncSession) -> int:  # ruff: ignore[line-too-long]
    """Re-create recent throttle state after a restart from the audit log, so restarting the server doesn't hand an attacker a fresh set of attempts. Returns rows used."""  # ruff: ignore[docstring-missing-returns, line-too-long]
    from datetime import timedelta

    from sqlalchemy import select

    from smp.core.db import utcnow
    from smp.core.models import AuditLog

    now = utcnow()
    since = now - timedelta(seconds=max(FAILURE_MEMORY_SECONDS, throttle.lockout, 60.0))  # ruff: ignore[line-too-long]
    rows = (
        await db.execute(
            select(AuditLog.action, AuditLog.ip, AuditLog.actor_name, AuditLog.at)
            .where(AuditLog.action.in_(("core.auth.login", "core.auth.login_failed", "core.auth.password_change_failed")),  # ruff: ignore[line-too-long]
                   AuditLog.at >= since)
            .order_by(AuditLog.at)
        )
    ).all()

    for action, ip_, username, at in rows:
        ago = (now - at).total_seconds()
        if ago <= 60:
            throttle.record_attempt(ip_ or "unknown", seconds_ago=ago)
        # One counter per account
        if action in {"core.auth.login_failed", "core.auth.password_change_failed"}:  # ruff: ignore[line-too-long]
            # Replayed in order: lockouts too
            throttle.record_failure(username or "", seconds_ago=ago)
        elif action == "core.auth.login":
            throttle.record_success(username or "")

    return len(rows)
