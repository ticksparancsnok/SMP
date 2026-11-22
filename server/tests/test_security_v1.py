# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Second security review (v1.0.0): key handling and rotation, throttle persistence,
fail-fast config, audit fallback, password whitespace, unique e-mail, OTP lifecycle."""

import logging
from datetime import timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from smp.core.config import Settings, get_settings
from smp.core.db import utcnow
from smp.core.events import InProcessEventBus
from smp.core.models import User
from smp.core.module import ModuleRegistry
from smp.core.otp import clear_otp, otp_state, readable_otp
from smp.core.passwords import password_problem
from smp.core.security import decrypt_secret, encrypt_secret, reencrypt_secret
from smp.main import create_app
from smp.modules import all_modules
from tests.conftest import ADMIN_PW

KEY_A = "first-secret-key-0123456789-abcdefghijklmnop"
KEY_B = "second-secret-key-9876543210-zyxwvutsrqponml"


@pytest.fixture
def keys():
    """Let a test switch keys; always restore the suite's key."""
    s = get_settings()
    saved = (s.secret_key, s.secret_key_previous)
    yield s
    s.secret_key, s.secret_key_previous = saved


def client_from(app, ip: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 40000)), base_url="http://test")


# --------------------------------------------------------------------------- configuration


def test_secret_key_validation():
    assert Settings(secret_key="").secret_key_problem() == "SMP_SECRET_KEY (or SMP_SECRET_KEY_FILE) is not set"
    assert "at least 32" in Settings(secret_key="x" * 31).secret_key_problem()
    assert "placeholder" in Settings(secret_key="change-me" * 5).secret_key_problem()
    assert Settings(secret_key=KEY_A).secret_key_problem() is None


def test_secret_key_file(tmp_path):
    f = tmp_path / "smp_secret"
    f.write_text(KEY_A + "\n")
    assert Settings(secret_key="", secret_key_file=str(f)).secret_key == KEY_A


def test_hash_concurrency_setting():
    assert Settings(hash_concurrency=7).effective_hash_concurrency() == 7
    assert Settings(hash_concurrency=0).effective_hash_concurrency() >= 1


async def test_server_refuses_to_start_without_key(migrated_db, keys):
    keys.secret_key = ""
    app = create_app(registry=ModuleRegistry(all_modules()), bus=InProcessEventBus())
    with pytest.raises(RuntimeError, match="refusing to start"):
        async with app.router.lifespan_context(app):
            pass


# --------------------------------------------------------------------------- key rotation


def test_key_rotation_keeps_otps_readable(keys, caplog):
    keys.secret_key = KEY_A
    token = encrypt_secret("abcd-efgh-jkmn")
    # rotate: new key, old one kept as "previous" -> still readable
    keys.secret_key, keys.secret_key_previous = KEY_B, KEY_A
    assert decrypt_secret(token) == "abcd-efgh-jkmn"
    rotated = reencrypt_secret(token)
    # old key removed: re-encrypted token works, the original one doesn't (and is logged)
    keys.secret_key_previous = ""
    assert decrypt_secret(rotated) == "abcd-efgh-jkmn"
    with caplog.at_level(logging.WARNING):
        assert decrypt_secret(token) is None
    assert "could not be decrypted" in caplog.text and "abcd" not in caplog.text


async def test_reencrypt_otps_cli(admin, keys):
    from argparse import Namespace

    from smp.cli import _reencrypt_otps

    u = (await admin.post("/api/v1/users", json={"username": "rot.cli"})).json()
    keys.secret_key, keys.secret_key_previous = KEY_B, keys.secret_key
    await _reencrypt_otps(Namespace())
    keys.secret_key_previous = ""
    export = await admin.get("/api/v1/users/credentials.xlsx")
    assert u["one_time_password"].encode() in _xlsx_cells(export.content)


def _xlsx_cells(data: bytes) -> bytes:
    import io

    from openpyxl import load_workbook

    ws = load_workbook(io.BytesIO(data)).active
    return "|".join(str(c.value) for row in ws.iter_rows() for c in row).encode()


# --------------------------------------------------------------------------- throttle persistence


async def test_throttle_survives_restart(app, admin):
    async with client_from(app, "10.9.9.9") as attacker:
        for _ in range(4):  # one short of the default lockout (5)
            assert (await attacker.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})).status_code == 401

    # "restart": a brand-new app with an empty in-memory throttle, seeded on startup
    fresh = create_app(registry=ModuleRegistry(all_modules()), bus=InProcessEventBus())
    async with fresh.router.lifespan_context(fresh):
        async with client_from(fresh, "10.8.8.8") as attacker:
            # the 4 earlier failures still count: the 5th locks the account, from any IP
            r = await attacker.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
            assert r.status_code == 401
            r = await attacker.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW})
            assert r.status_code == 429

    # and a lockout itself survives a restart too
    again = create_app(registry=ModuleRegistry(all_modules()), bus=InProcessEventBus())
    async with again.router.lifespan_context(again):
        async with client_from(again, "10.7.7.1") as real_admin:
            r = await real_admin.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW})
            assert r.status_code == 429


# --------------------------------------------------------------------------- audit fallback


async def test_failed_login_audit_commit_failure_still_401(app, admin, monkeypatch, caplog):
    real_commit = AsyncSession.commit

    async def broken_commit(self):
        raise RuntimeError("database went away")

    monkeypatch.setattr(AsyncSession, "commit", broken_commit)
    try:
        async with client_from(app, "10.7.7.7") as c:
            with caplog.at_level(logging.ERROR):
                r = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    finally:
        monkeypatch.setattr(AsyncSession, "commit", real_commit)
    assert r.status_code == 401
    assert "could not write login_failed audit entry" in caplog.text and "10.7.7.7" in caplog.text


# --------------------------------------------------------------------------- input validation


def test_password_surrounding_whitespace_rejected():
    assert password_problem(" Tavaszi-Szel-42").code == "password.surrounding_space"
    assert password_problem("Tavaszi-Szel-42 ").code == "password.surrounding_space"
    assert "space" in str(password_problem(" Tavaszi-Szel-42"))  # English message for the CLI
    # Values in the message travel as params, so the UI can translate it.
    short = password_problem("abc12")
    assert short.code == "password.too_short" and short.params == {"min": 8} and "8 characters" in short.message
    assert password_problem("Tavaszi Szel 42") is None  # inner spaces are fine (passphrases)


async def test_email_unique_per_school(admin):
    r = await admin.post("/api/v1/users", json={"username": "mail.a", "email": "Kiss.Peter@iskola.hu"})
    assert r.status_code == 201
    a = r.json()
    r = await admin.post("/api/v1/users", json={"username": "mail.b", "email": "kiss.peter@ISKOLA.hu"})
    assert r.status_code == 409 and "e-mail" in r.json()["detail"] and r.json()["code"] == "users.email_taken"
    b = (await admin.post("/api/v1/users", json={"username": "mail.b"})).json()
    assert (await admin.patch(f"/api/v1/users/{b['id']}", json={"email": "kiss.peter@iskola.hu"})).status_code == 409
    # keeping your own address, or having none, is fine
    assert (await admin.patch(f"/api/v1/users/{a['id']}", json={"email": "kiss.peter@iskola.hu"})).status_code == 200
    assert (await admin.post("/api/v1/users", json={"username": "mail.c"})).status_code == 201


# --------------------------------------------------------------------------- OTP lifecycle (unit)


def test_otp_lifecycle_unit(keys):
    keys.secret_key = KEY_A
    now = utcnow()
    u = User(username="unit", otp_secret=encrypt_secret("abcd-efgh-jkmn"), otp_expires_at=now + timedelta(days=1))
    assert otp_state(u, now) == "active" and readable_otp(u, now) == "abcd-efgh-jkmn"
    assert otp_state(u, now + timedelta(days=2)) == "expired"
    assert readable_otp(u, now + timedelta(days=2)) is None  # expired OTPs are never exported
    clear_otp(u)
    assert otp_state(u, now) is None and readable_otp(u, now) is None
