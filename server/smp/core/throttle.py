"""Login throttling (replaces the per-account lockout).

A lockout stored on the account lets anyone lock out the admin from anywhere, and
"locked" vs "wrong password" answers tell an attacker which usernames exist. Instead:

  * failures are counted per (client IP, username): after `max_failures` within the
    window that IP can't try that username until the window passes;
  * every attempt is counted per IP: more than `ip_per_minute` per minute is refused
    before any password hashing happens (caps CPU/RAM use by argon2);
  * both apply identically to existing and non-existing usernames.

State is in memory, per server process (SMP runs one process). It survives restarts
because every counted attempt is also in the audit log, and the server re-seeds the
throttle from it on startup (see seed_throttle_from_audit). Behind a reverse proxy
the client IP must be the real one (uvicorn --forwarded-allow-ips), otherwise
everybody shares the proxy's IP.
"""

import time
from collections import deque


class LoginThrottle:
    def __init__(self, max_failures: int, window_seconds: float, ip_per_minute: int, clock=time.monotonic):
        self.max_failures = max_failures
        self.window = window_seconds
        self.ip_per_minute = ip_per_minute
        self._clock = clock
        self._failures: dict[tuple[str, str], deque[float]] = {}
        self._attempts: dict[str, deque[float]] = {}
        self._calls = 0

    @staticmethod
    def _key(ip: str, username: str) -> tuple[str, str]:
        return ip, username.strip().casefold()

    @staticmethod
    def _prune(q: deque[float], older_than: float) -> None:
        while q and q[0] <= older_than:
            q.popleft()

    def _sweep(self, now: float) -> None:
        """Drop empty entries now and then so memory stays bounded."""
        self._calls += 1
        if self._calls % 1000:
            return
        for store, horizon in ((self._failures, self.window), (self._attempts, 60.0)):
            for k in list(store):
                self._prune(store[k], now - horizon)
                if not store[k]:
                    del store[k]

    def allowed(self, ip: str, username: str) -> bool:
        now = self._clock()
        self._sweep(now)
        attempts = self._attempts.get(ip)
        if attempts is not None:
            self._prune(attempts, now - 60.0)
            if len(attempts) >= self.ip_per_minute:
                return False
        failures = self._failures.get(self._key(ip, username))
        if failures is not None:
            self._prune(failures, now - self.window)
            if len(failures) >= self.max_failures:
                return False
        return True

    def record_attempt(self, ip: str, seconds_ago: float = 0.0) -> None:
        self._attempts.setdefault(ip, deque()).append(self._clock() - seconds_ago)

    def record_failure(self, ip: str, username: str, seconds_ago: float = 0.0) -> None:
        self._failures.setdefault(self._key(ip, username), deque()).append(self._clock() - seconds_ago)

    def record_success(self, ip: str, username: str) -> None:
        self._failures.pop(self._key(ip, username), None)

    def reset(self) -> None:
        self._failures.clear()
        self._attempts.clear()


async def seed_throttle_from_audit(throttle: LoginThrottle, db) -> int:
    """Re-create recent throttle state after a restart from the audit log, so restarting
    the server doesn't hand an attacker a fresh set of attempts. Returns rows used."""
    from datetime import timedelta

    from sqlalchemy import select

    from smp.core.db import utcnow
    from smp.core.models import AuditLog

    now = utcnow()
    since = now - timedelta(seconds=max(throttle.window, 60.0))
    rows = (
        await db.execute(
            select(AuditLog.action, AuditLog.ip, AuditLog.actor_name, AuditLog.at)
            .where(AuditLog.action.in_(("core.auth.login", "core.auth.login_failed")), AuditLog.at >= since)
            .order_by(AuditLog.at)
        )
    ).all()
    for action, ip, username, at in rows:
        ago = (now - at).total_seconds()
        ip = ip or "unknown"
        if ago <= 60:
            throttle.record_attempt(ip, seconds_ago=ago)
        if action == "core.auth.login_failed" and ago <= throttle.window:
            throttle.record_failure(ip, username or "", seconds_ago=ago)
        elif action == "core.auth.login":
            throttle.record_success(ip, username or "")
    return len(rows)
