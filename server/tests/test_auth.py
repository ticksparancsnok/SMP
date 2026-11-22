# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Authentication hardening: one-time passwords, login throttling, password policy,
session rotation, upload limits. Section names refer to the security review IDs."""

import io
import re

import httpx
from openpyxl import load_workbook
from sqlalchemy import select, text

from smp.core import db
from smp.core.config import get_settings
from smp.core.models import User
from smp.core.throttle import LoginThrottle
from tests.conftest import ADMIN_PW, login_as, make_client


def client_from(app, ip: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 40000)), base_url="http://test")


async def login(c, username, password):
    return await c.post("/api/v1/auth/login", json={"username": username, "password": password})


def credentials(xlsx_bytes: bytes) -> dict[str, tuple]:
    ws = load_workbook(io.BytesIO(xlsx_bytes)).active
    rows = [tuple(c.value for c in r) for r in ws.iter_rows()]
    assert rows[0] == ("Name", "Username", "Class", "One-time password", "Valid until")  # API default: English
    return {r[1]: r for r in rows[1:]}


# --------------------------------------------------------------------------- SEC-01 / SEC-08: individual OTPs


async def test_new_users_get_individual_one_time_passwords(app, admin):
    a = (await admin.post("/api/v1/users", json={"username": "otp.a", "roles": [{"role": "student"}]})).json()
    b = (await admin.post("/api/v1/users", json={"username": "otp.b", "roles": [{"role": "student"}]})).json()
    otp_a, otp_b = a["one_time_password"], b["one_time_password"]
    assert otp_a and otp_b and otp_a != otp_b and len(otp_a) == 14  # xxxx-xxxx-xxxx
    assert a["must_change_password"] is True and a["otp_status"] == "active"

    async with make_client(app) as c:
        # knowing your own OTP gets you nothing on somebody else's account (SEC-01)
        assert (await login(c, "otp.b", otp_a)).status_code == 401
        me = (await login(c, "otp.b", otp_b)).json()
        assert me["must_change_password"] is True

    # stored encrypted, not in plaintext; the hash is the login credential
    async with db.sessionmaker()() as s:
        u = (await s.execute(select(User).where(User.username == "otp.a"))).scalar_one()
        assert u.otp_secret and otp_a not in u.otp_secret and u.password_hash.startswith("$argon2")
    # the audit log records that an OTP was issued, never the value
    assert otp_a not in str((await admin.get("/api/v1/audit")).json())


async def test_otp_is_single_use_and_cleared_on_change(app, admin):
    u = (await admin.post("/api/v1/users", json={"username": "otp.c"})).json()
    otp = u["one_time_password"]
    async with make_client(app) as c:
        await login(c, "otp.c", otp)
        r = await c.post("/api/v1/auth/password", json={"current_password": otp, "new_password": "Tavaszi-Szel-42"})
        assert r.status_code == 204
    async with make_client(app) as c:
        assert (await login(c, "otp.c", otp)).status_code == 401  # the OTP no longer works
        assert (await login(c, "otp.c", "Tavaszi-Szel-42")).status_code == 200
    got = (await admin.get(f"/api/v1/users/{u['id']}")).json()
    assert got["otp_status"] is None and got["must_change_password"] is False
    assert "otp.c" not in credentials((await admin.get("/api/v1/users/credentials.xlsx")).content)


async def test_expired_otp_cannot_log_in(app, admin):
    otp = (await admin.post("/api/v1/users", json={"username": "otp.old"})).json()["one_time_password"]
    async with db.sessionmaker()() as s:
        await s.execute(text("UPDATE users SET otp_expires_at = now() - interval '1 minute' WHERE username = 'otp.old'"))
        await s.commit()
    async with make_client(app) as c:
        r = await login(c, "otp.old", otp)
        assert r.status_code == 401 and r.json()["detail"] == "invalid username or password"
        assert r.json()["code"] == "auth.invalid_credentials"
    entry = (await admin.get("/api/v1/audit", params={"action": "core.auth.login_failed"})).json()[0]
    assert entry["details"]["reason"] == "one-time password expired"
    listed = (await admin.get("/api/v1/users", params={"q": "otp.old"})).json()["items"][0]
    assert listed["otp_status"] == "expired"
    assert "otp.old" not in credentials((await admin.get("/api/v1/users/credentials.xlsx")).content)
    # admin issues a new one
    r = await admin.post(f"/api/v1/users/{listed['id']}/password", json={})
    new_otp = r.json()["one_time_password"]
    assert r.status_code == 200 and new_otp and new_otp != otp
    async with make_client(app) as c:
        assert (await login(c, "otp.old", new_otp)).status_code == 200


async def test_activation_state_is_admin_only(app, admin):
    await admin.post("/api/v1/users", json={"username": "otp.d"})
    async with login_as(app, admin, "tanar_otp", [{"role": "teacher"}]) as teacher:
        u = (await teacher.get("/api/v1/users", params={"q": "otp.d"})).json()["items"][0]
        assert u["must_change_password"] is None and u["otp_status"] is None and u["otp_expires_at"] is None
        # teachers may export slips, but only for students of groups they teach (none here)
        assert credentials((await teacher.get("/api/v1/users/credentials.xlsx")).content) == {}


async def test_credentials_export(app, admin):
    g = (await admin.post("/api/v1/groups", json={"name": "7.C", "kind": "class"})).json()
    a = (await admin.post("/api/v1/users", json={"username": "slip.a", "display_name": "Slip A", "group_ids": [g["id"]]})).json()
    await admin.post("/api/v1/users", json={"username": "slip.b"})
    await admin.post("/api/v1/users", json={"username": "slip.own", "password": "Tavaszi-Szel-42"})  # no OTP

    rows = credentials((await admin.get("/api/v1/users/credentials.xlsx")).content)
    assert set(rows) == {"slip.a", "slip.b"}
    assert rows["slip.a"][:4] == ("Slip A", "slip.a", "7.C", a["one_time_password"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", rows["slip.a"][4])  # valid-until, English format
    # Hungarian headers and date format on request
    hu = await admin.get("/api/v1/users/credentials.xlsx", params={"lang": "hu"})
    hu_rows = [tuple(c.value for c in r) for r in load_workbook(io.BytesIO(hu.content)).active.iter_rows()]
    assert hu_rows[0] == ("Név", "Felhasználónév", "Osztály", "Egyszeri jelszó", "Érvényes eddig")
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\. \d{2}:\d{2}", {r[1]: r for r in hu_rows[1:]}["slip.a"][4])
    assert 'filename="belepesi-adatok-' in hu.headers["content-disposition"]
    # same filters as the list
    rows = credentials((await admin.get("/api/v1/users/credentials.xlsx", params={"group_id": g["id"]})).content)
    assert set(rows) == {"slip.a"}
    entry = (await admin.get("/api/v1/audit", params={"action": "core.user.credentials_export"})).json()[0]
    assert entry["details"]["usernames"] == ["slip.a"]
    assert a["one_time_password"] not in str(entry)


async def test_admin_reset_custom_password(app, admin):
    u = (await admin.post("/api/v1/users", json={"username": "reset.x", "roles": [{"role": "student"}]})).json()
    r = await admin.post(f"/api/v1/users/{u['id']}/password", json={"password": "abc"})
    assert r.status_code == 422
    r = await admin.post(f"/api/v1/users/{u['id']}/password", json={"password": "Ideiglenes-77"})
    assert r.status_code == 200 and r.json()["one_time_password"] is None
    got = (await admin.get(f"/api/v1/users/{u['id']}")).json()
    assert got["must_change_password"] is True and got["otp_status"] is None  # the old OTP is gone
    async with make_client(app) as c:
        assert (await login(c, "reset.x", u["one_time_password"])).status_code == 401
        assert (await login(c, "reset.x", "Ideiglenes-77")).json()["must_change_password"] is True


async def test_missing_secret_key_is_503_not_silent(admin):
    settings = get_settings()
    saved, settings.secret_key = settings.secret_key, ""
    try:
        r = await admin.post("/api/v1/users", json={"username": "nokey"})
        assert r.status_code == 503 and "SMP_SECRET_KEY" in r.json()["detail"]
        assert r.json()["code"] == "otp.secret_key_missing"
        # an explicit password still works without the key
        r = await admin.post("/api/v1/users", json={"username": "withpw", "password": "Tavaszi-Szel-42"})
        assert r.status_code == 201
    finally:
        settings.secret_key = saved
    assert (await admin.get("/api/v1/users", params={"q": "nokey"})).json()["total"] == 0


# --------------------------------------------------------------------------- SEC-02 / SEC-03: throttling


async def test_account_lockout_across_ips_and_reveals_nothing(app, admin):
    """5 failed logins for one account (defaults) lock it for 60 s, whichever IPs they
    come from: rotating addresses (e.g. inside an IPv6 /64) doesn't help an attacker."""
    throttle = app.state.login_throttle
    now = [10_000.0]
    saved_clock, throttle._clock = throttle._clock, lambda: now[0]
    try:
        assert (throttle.max_failures, throttle.lockout) == (5, 60)
        for name in ("admin", "nobody-here"):
            for i in range(5):
                async with client_from(app, f"2001:db8::{name[0]}{i}") as c:
                    assert (await login(c, name, "wrong")).status_code == 401
            # 6th try, from yet another address: same answer for an existing and a
            # non-existing user (no oracle) ...
            async with client_from(app, "2001:db8::99") as c:
                assert (await login(c, name, "wrong")).status_code == 429
        # ... and even the right password is refused while the lockout lasts
        async with client_from(app, "10.2.2.2") as real_admin:
            assert (await login(real_admin, "admin", ADMIN_PW)).status_code == 429
            now[0] += 59
            assert (await login(real_admin, "admin", ADMIN_PW)).status_code == 429
            now[0] += 2  # 61 s later: the lockout is over
            assert (await login(real_admin, "admin", ADMIN_PW)).status_code == 200
    finally:
        throttle._clock = saved_clock


async def test_ip_attempt_cap_stops_before_hashing_and_logging(app, admin):
    throttle = app.state.login_throttle
    saved, throttle.ip_per_minute = throttle.ip_per_minute, 5
    try:
        before = len((await admin.get("/api/v1/audit", params={"action": "core.auth.login_failed", "limit": 1000})).json())
        async with client_from(app, "10.3.3.3") as flood:
            codes = [(await login(flood, f"user{i}", "x")).status_code for i in range(8)]
        assert codes == [401] * 5 + [429] * 3
        after = len((await admin.get("/api/v1/audit", params={"action": "core.auth.login_failed", "limit": 1000})).json())
        assert after - before == 5  # refused attempts don't write audit rows
    finally:
        throttle.ip_per_minute = saved


def test_throttle_lockout_rules():
    from smp.core.config import Settings
    from smp.core.throttle import FAILURE_MEMORY_SECONDS

    defaults = Settings.model_fields
    assert defaults["login_max_failures"].default == 5 and defaults["login_lockout_seconds"].default == 60

    now = [1000.0]
    t = LoginThrottle(max_failures=2, lockout_seconds=60, ip_per_minute=100, clock=lambda: now[0])
    t.record_failure("Kiss.Peter")
    assert t.allowed("ip", "kiss.peter")
    t.record_failure("kiss.peter ")
    assert not t.allowed("ip", "kiss.peter") and not t.allowed("other-ip", "KISS.PETER")  # per account, any IP
    now[0] += 61
    assert t.allowed("ip", "kiss.peter")
    t.record_failure("kiss.peter")  # a lockout starts a fresh count
    assert t.allowed("ip", "kiss.peter")

    t.record_failure("x")
    t.record_success("x")
    t.record_failure("x")
    assert t.allowed("ip", "x")  # success cleared the earlier failure

    t.record_failure("slow")
    now[0] += FAILURE_MEMORY_SECONDS + 1
    t.record_failure("slow")
    assert t.allowed("ip", "slow")  # failures that old no longer count


# --------------------------------------------------------------------------- SEC-06: password policy


async def test_password_policy(app, admin):
    async with login_as(app, admin, "policy.user", [{"role": "student"}]) as c:
        from tests.conftest import OWN_PW

        for weak, code in [
            ("password123", "password.too_common"),
            ("Iskola2026", "password.too_common"),
            ("abcdefghij", "password.letters_and_digits"),
            ("aaaa1111", "password.too_few_distinct"),
            ("policy.user1", "password.contains_username"),
        ]:
            r = await c.post("/api/v1/auth/password", json={"current_password": OWN_PW, "new_password": weak})
            body = r.json()
            # English detail for API clients, stable code (+ params) for the UI to translate
            assert r.status_code == 400 and body["code"] == code and body["detail"].startswith(("The ", "This ")), weak
        r = await c.post("/api/v1/auth/password", json={"current_password": "wrong-pw-1", "new_password": "Tavaszi-Szel-42"})
        assert r.status_code == 400 and r.json()["code"] == "auth.wrong_current_password"
    r = await admin.post("/api/v1/users", json={"username": "weak.admin.pw", "password": "12345678"})
    assert r.status_code == 422 and r.json()["code"] == "password.letters_and_digits"


# --------------------------------------------------------------------------- SEC-07: session rotation


async def test_password_change_rotates_session(app, admin):
    otp = (await admin.post("/api/v1/users", json={"username": "rot.u"})).json()["one_time_password"]
    async with make_client(app) as c, make_client(app) as other_device:
        await login(c, "rot.u", otp)
        await login(other_device, "rot.u", otp)
        old_cookie = c.cookies.get("smp_session")
        r = await c.post("/api/v1/auth/password", json={"current_password": otp, "new_password": "Tavaszi-Szel-42"})
        assert r.status_code == 204
        new_cookie = c.cookies.get("smp_session")
        assert new_cookie and new_cookie != old_cookie
        assert (await c.get("/api/v1/auth/me")).status_code == 200  # continues on the new session
        assert (await other_device.get("/api/v1/auth/me")).status_code == 401
    async with make_client(app) as stale:
        stale.cookies.set("smp_session", old_cookie)
        assert (await stale.get("/api/v1/auth/me")).status_code == 401


# --------------------------------------------------------------------------- SEC-05: streamed upload limit


async def test_chunked_upload_is_cut_off(admin, monkeypatch):
    from smp.core.api import users

    monkeypatch.setattr(users, "MAX_IMPORT_BYTES", 10_000)

    async def body():
        for _ in range(100):  # 1 MB in 10 kB chunks, no Content-Length
            yield b"x" * 10_000

    r = await admin.post("/api/v1/users/import", content=body(), headers={"content-type": "application/octet-stream"})
    assert r.status_code == 413
    assert "content-length" not in {k.lower() for k in r.request.headers}

