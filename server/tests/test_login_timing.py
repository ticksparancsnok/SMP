# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Issue #4: failed logins must not reveal which accounts exist or are not activated yet.

One-time passwords are hashed with a lighter argon2 setting than chosen passwords, so a
wrong password for a not-yet-activated account used to answer faster than one for an
unknown username. The fix (commit f7e578c) pads every failed login up to
LOGIN_PENALTY_SECONDS, sleeping without blocking the event loop.

These tests check that the goal is met (every failure path takes the same time) and that
nothing else changed (status codes, error codes, audit entries, lockout, fast and working
successful logins, other requests not held up).

These tests use a 1 s penalty, the real setting at the time of writing, whatever
LOGIN_PENALTY_SECONDS is changed to later. A shorter one is too tight with `pytest -n
auto`: the other workers compete for the CPU and an argon2 check then takes ~300 ms
instead of ~70 ms. test_default_penalty_covers_the_slowest_hash checks the real value."""

import asyncio
import statistics
import time

import httpx
import pytest
from sqlalchemy import select, text

from smp.core import db, security
from smp.core.api import auth as auth_api
from smp.core.models import AuditLog
from tests.conftest import ADMIN_PW, make_client

PENALTY = 1.0  # seconds; well above an argon2 check even on a busy machine
# How far apart the failure paths may be. Far below the ~40 ms OTP-vs-unknown gap the
# issue describes would be ideal, but CI machines jitter; 80 ms still catches a path
# that skips the padding (it would answer ~PENALTY faster).
TOLERANCE = 0.08
RUNS = 3  # per failure path; the median smooths out scheduler noise

WRONG = "definitely-not-the-password"

# The real setting, read at import: conftest.py sets it to 0 while each test runs.
REAL_PENALTY = auth_api.LOGIN_PENALTY_SECONDS


@pytest.fixture
def penalty(monkeypatch):
    # Overrides conftest.py's no_login_penalty (it runs first, being autouse).
    # login() reads the module global on every call, so patching it takes effect at once.
    monkeypatch.setattr(auth_api, "LOGIN_PENALTY_SECONDS", PENALTY)
    return PENALTY


def client_from(app, ip: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 40000)), base_url="http://test")


async def post_login(c: httpx.AsyncClient, username: str, password: str, org: str | None = None):
    body = {"username": username, "password": password}
    if org is not None:
        body["org"] = org
    start = time.perf_counter()
    r = await c.post("/api/v1/auth/login", json=body)
    return r, time.perf_counter() - start


async def create_user(admin: httpx.AsyncClient, username: str) -> str:
    """A not-yet-activated account: it still has its (lightly hashed) one-time password."""
    r = await admin.post("/api/v1/users", json={"username": username, "roles": [{"role": "student"}]})
    assert r.status_code == 201, r.text
    return r.json()["one_time_password"]


async def activate(app, username: str, otp: str, new_password: str) -> None:
    """Log in with the OTP and choose an own password: the account now has a full-cost hash."""
    async with make_client(app) as c:
        r, _ = await post_login(c, username, otp)
        assert r.status_code == 200, r.text
        r = await c.post("/api/v1/auth/password", json={"current_password": otp, "new_password": new_password})
        assert r.status_code == 204, r.text


async def failure_cases(app, admin) -> dict[str, tuple[str, str, str | None]]:
    """Every way a login can fail with 401: name -> (username, password, org)."""
    pending_otp = await create_user(admin, "timing.pending")

    active_otp = await create_user(admin, "timing.active")
    await activate(app, "timing.active", active_otp, "Timing-Own-Passw0rd!")

    disabled_otp = await create_user(admin, "timing.disabled")
    expired_otp = await create_user(admin, "timing.expired")
    async with db.sessionmaker()() as s:
        await s.execute(text("UPDATE users SET disabled = true WHERE username = 'timing.disabled'"))
        await s.execute(
            text("UPDATE users SET otp_expires_at = now() - interval '1 minute' WHERE username = 'timing.expired'")
        )
        await s.commit()

    return {
        # the oracle from issue #4: these two used to differ by the OTP hash's lower cost
        "unknown user": ("timing.nobody", WRONG, None),
        "not activated, wrong password": ("timing.pending", WRONG, None),
        "activated, wrong password": ("timing.active", WRONG, None),
        "disabled, right password": ("timing.disabled", disabled_otp, None),
        "expired one-time password": ("timing.expired", expired_otp, None),
        "unknown school": ("timing.pending", pending_otp, "no-such-school"),
    }


# --------------------------------------------------------------------------- the goal


async def test_every_failed_login_takes_the_same_time(app, admin, penalty):
    cases = await failure_cases(app, admin)
    medians: dict[str, float] = {}
    for name, (username, password, org) in cases.items():
        times = []
        for i in range(RUNS):
            app.state.login_throttle.reset()  # measure the login itself, never a lockout
            async with client_from(app, f"10.44.{len(medians)}.{i}") as c:
                r, elapsed = await post_login(c, username, password, org)
            # previous behaviour: one indistinguishable answer for every failure
            assert r.status_code == 401, (name, r.text)
            assert r.json()["code"] == "auth.invalid_credentials", (name, r.text)
            times.append(elapsed)
        medians[name] = statistics.median(times)

    report = ", ".join(f"{n}: {t * 1000:.0f} ms" for n, t in medians.items())
    for name, t in medians.items():
        assert t >= penalty, f"{name} answered before the penalty ({report})"
    spread = max(medians.values()) - min(medians.values())
    assert spread < TOLERANCE, f"failure paths differ by {spread * 1000:.0f} ms ({report})"
    # The exact pair from the issue, held to the same bound on its own.
    gap = abs(medians["not activated, wrong password"] - medians["unknown user"])
    assert gap < TOLERANCE, f"not-activated vs unknown differ by {gap * 1000:.0f} ms ({report})"


async def test_locked_accounts_take_the_penalty_too(app, admin, penalty):
    """A 429 for a locked account must not answer instantly either (unlocked ones take PENALTY)."""
    await create_user(admin, "timing.locked")
    throttle = app.state.login_throttle
    for i in range(throttle.max_failures):
        async with client_from(app, f"10.45.0.{i}") as c:
            r, _ = await post_login(c, "timing.locked", WRONG)
            assert r.status_code == 401
    async with client_from(app, "10.45.1.1") as c:
        r, elapsed = await post_login(c, "timing.locked", WRONG)
    assert r.status_code == 429 and r.json()["code"] == "auth.too_many_attempts", r.text
    assert elapsed >= penalty


async def test_default_penalty_covers_the_slowest_hash():
    """With the real setting, the padding must swallow the full-cost argon2 check, or the
    old difference shows through again (sleep() with a negative time returns at once).

    The fastest of a few checks is the hash's own cost: with `pytest -n auto` the other
    workers compete for the CPU and an average comes out several times higher."""
    times = []
    for _ in range(5):
        start = time.perf_counter()
        security.verify_password(security._DUMMY_HASH, WRONG)  # the expensive, chosen-password hash
        times.append(time.perf_counter() - start)
    hash_cost = min(times)
    assert REAL_PENALTY >= 3 * hash_cost, (
        f"LOGIN_PENALTY_SECONDS={REAL_PENALTY} is too close to an argon2 check ({hash_cost * 1000:.0f} ms)"
    )


# --------------------------------------------------------------------------- previous behaviour


async def test_successful_logins_still_work_and_are_not_delayed(app, admin, penalty):
    otp = await create_user(admin, "timing.ok")
    async with make_client(app) as c:
        r, elapsed = await post_login(c, "timing.ok", otp)
        assert r.status_code == 200, r.text
        assert r.json()["must_change_password"] is True
        assert elapsed < penalty, f"a correct OTP login took {elapsed * 1000:.0f} ms"
        r = await c.post("/api/v1/auth/password", json={"current_password": otp, "new_password": "Timing-Ok-Passw0rd!"})
        assert r.status_code == 204, r.text

    async with make_client(app) as c:
        r, elapsed = await post_login(c, "timing.ok", "Timing-Ok-Passw0rd!")
        assert r.status_code == 200, r.text
        assert r.json()["must_change_password"] is False
        assert elapsed < penalty, f"a correct login took {elapsed * 1000:.0f} ms"
        assert (await c.get("/api/v1/auth/me")).status_code == 200  # the session works

    async with make_client(app) as c:
        r, elapsed = await post_login(c, "admin", ADMIN_PW)
        assert r.status_code == 200 and elapsed < penalty


async def test_failed_logins_are_still_audited_with_their_reason(app, admin, penalty):
    cases = await failure_cases(app, admin)
    app.state.login_throttle.reset()
    for i, (username, password, org) in enumerate(cases.values()):
        async with client_from(app, f"10.46.0.{i}") as c:
            r, _ = await post_login(c, username, password, org)
            assert r.status_code == 401

    async with db.sessionmaker()() as s:
        rows = (
            await s.execute(select(AuditLog.actor_name, AuditLog.details).where(AuditLog.action == "core.auth.login_failed"))
        ).all()
    reasons = {(name, (details or {}).get("reason")) for name, details in rows}
    assert ("timing.nobody", "unknown or disabled user") in reasons
    assert ("timing.disabled", "unknown or disabled user") in reasons
    assert ("timing.pending", "bad password") in reasons
    assert ("timing.active", "bad password") in reasons
    assert ("timing.expired", "one-time password expired") in reasons
    # An unknown school has no org to log against, as before: no entry, still a 401.


async def test_failed_logins_still_count_towards_a_lockout(app, admin, penalty):
    otp = await create_user(admin, "timing.lockout")
    throttle = app.state.login_throttle
    for i in range(throttle.max_failures):
        async with client_from(app, f"10.47.0.{i}") as c:
            assert (await post_login(c, "timing.lockout", WRONG))[0].status_code == 401
    async with client_from(app, "10.47.1.1") as c:
        r, _ = await post_login(c, "timing.lockout", otp)  # even the right password
    assert r.status_code == 429


# --------------------------------------------------------------------------- not blocking


async def test_waiting_does_not_block_other_requests(app, admin, penalty):
    """The padding is an asyncio sleep: concurrent failed logins wait side by side, and a
    correct login meanwhile is answered right away."""
    otp = await create_user(admin, "timing.concurrent")
    app.state.login_throttle.reset()

    async def failed(i: int) -> float:
        async with client_from(app, f"10.48.0.{i}") as c:
            r, elapsed = await post_login(c, f"timing.nobody{i}", WRONG)
            assert r.status_code == 401
            return elapsed

    async def succeeded() -> float:
        await asyncio.sleep(0.05)  # start while the failed ones are waiting
        async with client_from(app, "10.48.1.1") as c:
            r, _ = await post_login(c, "timing.concurrent", otp)
            assert r.status_code == 200, r.text
        # Measured from the common start, not from when this request got going: a
        # blocking sleep would hold up the event loop before this request even starts.
        return time.perf_counter() - start

    start = time.perf_counter()
    *failed_times, ok_done = await asyncio.gather(*(failed(i) for i in range(4)), succeeded())
    total = time.perf_counter() - start

    assert all(t >= penalty for t in failed_times)
    assert total < 2 * penalty, f"4 failed logins took {total * 1000:.0f} ms in all: they ran one after another"
    assert ok_done < penalty, f"a correct login finished {ok_done * 1000:.0f} ms in, held up by failed ones"
