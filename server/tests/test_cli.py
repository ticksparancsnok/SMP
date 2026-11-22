"""The admin CLI as install.sh uses it: preflight, status, bootstrap --otp.

Run as real subprocesses (the commands manage their own DB engine) against the
test database, in an organization of their own ("cli-school")."""

import os
import subprocess
import sys

from sqlalchemy.engine import make_url

from tests.conftest import TEST_DB_URL, make_client


def smp(*args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "smp", *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def kv(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def test_preflight_ok_and_explains_problems(org):
    r = smp("preflight", "--fail-delay", "0")
    assert r.returncode == 0 and "OK" in r.stdout

    r = smp("preflight", "--fail-delay", "0", SMP_SECRET_KEY="change-me-change-me-change-me-change-me")
    assert r.returncode == 1
    assert "SMP can't start" in r.stderr and "placeholder" in r.stderr and "install.sh" in r.stderr

    wrong_pw = make_url(TEST_DB_URL).set(password="not-the-password").render_as_string(hide_password=False)
    r = smp("preflight", "--fail-delay", "0", SMP_DATABASE_URL=wrong_pw)
    assert r.returncode == 1
    assert "wrong password" in r.stderr and "SMP_DB_PASSWORD" in r.stderr and "docker compose down -v" in r.stderr
    assert "not-the-password" not in r.stderr  # never echo secrets


async def test_bootstrap_with_otp_then_status(app, org):
    before = smp("status", "--org-slug", "cli-school")
    assert before.returncode == 0 and kv(before.stdout)["bootstrapped"] == "no"

    r = smp("bootstrap", "--otp", "--machine", "--org-slug", "cli-school",
            "--org-name", "Teszt Gimnázium és Kollégium", "--admin-username", "igazgato")
    assert r.returncode == 0, r.stderr
    out = kv(r.stdout)
    assert out["SMP_ADMIN_USERNAME"] == "igazgato" and out["SMP_SCHOOL_NAME"] == "Teszt Gimnázium és Kollégium"
    assert len(out["SMP_ADMIN_OTP"]) == 14 and out["SMP_ADMIN_OTP_VALID_UNTIL"]

    after = kv(smp("status", "--org-slug", "cli-school").stdout)
    assert after["bootstrapped"] == "yes" and after["school_name"] == "Teszt Gimnázium és Kollégium" and after["admins"] == "1"

    # running it again doesn't touch the existing admin (install.sh re-runs rely on this)
    again = smp("bootstrap", "--otp", "--machine", "--org-slug", "cli-school", "--org-name", "X", "--admin-username", "igazgato")
    assert again.returncode == 3 and "already exists" in again.stderr

    # the one-time password works once, and forces a password change
    async with make_client(app) as c:
        r = await c.post("/api/v1/auth/login", json={"org": "cli-school", "username": "igazgato", "password": out["SMP_ADMIN_OTP"]})
        assert r.status_code == 200 and r.json()["must_change_password"] is True
        assert r.json()["org_name"] == "Teszt Gimnázium és Kollégium"
        assert (await c.get("/api/v1/users")).status_code == 403  # nothing else until the password is changed
        r = await c.post("/api/v1/auth/password", json={"current_password": out["SMP_ADMIN_OTP"], "new_password": "Sajat-Jelszo-2026"})
        assert r.status_code == 204
        assert (await c.get("/api/v1/users")).status_code == 200
        # the school name also lands in the core settings (Settings page)
        assert (await c.get("/api/v1/modules/core/settings")).json()["school_name"] == "Teszt Gimnázium és Kollégium"


def test_bootstrap_rejects_empty_school_name(org):
    r = smp("bootstrap", "--otp", "--org-slug", "cli-empty", "--org-name", "   ")
    assert r.returncode != 0 and "can't be empty" in r.stderr
