"""Authentication hardening: one-time passwords, login throttling, password policy,
session rotation, upload limits. Section names refer to the security review IDs."""

import io

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
    assert rows[0] == ("Név", "Felhasználónév", "Osztály", "Egyszeri jelszó", "Érvényes eddig")
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
    assert rows["slip.a"][4]  # valid-until date
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
        # an explicit password still works without the key
        r = await admin.post("/api/v1/users", json={"username": "withpw", "password": "Tavaszi-Szel-42"})
        assert r.status_code == 201
    finally:
        settings.secret_key = saved
    assert (await admin.get("/api/v1/users", params={"q": "nokey"})).json()["total"] == 0


# --------------------------------------------------------------------------- SEC-02 / SEC-03: throttling


async def test_throttle_is_per_ip_and_username_and_reveals_nothing(app, admin):
    # SMP_LOGIN_MAX_FAILURES=3 in tests
    async with client_from(app, "10.1.1.1") as attacker:
        for name in ("admin", "nobody-here"):
            for _ in range(3):
                assert (await login(attacker, name, "wrong")).status_code == 401
            # 4th try: same answer for an existing and a non-existing user (no oracle) ...
            assert (await login(attacker, name, "wrong")).status_code == 429
        # ... and even the right password is refused from this IP for now
        assert (await login(attacker, "admin", ADMIN_PW)).status_code == 429
    # the real admin, elsewhere, is not locked out (no lockout DoS)
    async with client_from(app, "10.2.2.2") as real_admin:
        assert (await login(real_admin, "admin", ADMIN_PW)).status_code == 200


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


def test_throttle_window_expires():
    now = [1000.0]
    t = LoginThrottle(max_failures=2, window_seconds=60, ip_per_minute=100, clock=lambda: now[0])
    for _ in range(2):
        t.record_attempt("ip")
        t.record_failure("ip", "Kiss.Peter")
    assert not t.allowed("ip", "kiss.peter")  # usernames compared case-insensitively
    assert t.allowed("other-ip", "kiss.peter")
    now[0] += 61
    assert t.allowed("ip", "kiss.peter")
    t.record_failure("ip", "x")
    t.record_success("ip", "x")
    t.record_failure("ip", "x")
    assert t.allowed("ip", "x")  # success cleared the earlier failure


# --------------------------------------------------------------------------- SEC-06: password policy


async def test_password_policy(app, admin):
    async with login_as(app, admin, "policy.user", [{"role": "student"}]) as c:
        from tests.conftest import OWN_PW

        for weak, why in [
            ("password123", "gyakori"),
            ("Iskola2026", "gyakori"),
            ("abcdefghij", "betűt és számot"),
            ("aaaa1111", "különböző"),
            ("policy.user1", "felhasználónevet"),
        ]:
            r = await c.post("/api/v1/auth/password", json={"current_password": OWN_PW, "new_password": weak})
            assert r.status_code == 400 and why in r.json()["detail"], weak
    r = await admin.post("/api/v1/users", json={"username": "weak.admin.pw", "password": "12345678"})
    assert r.status_code == 422


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

