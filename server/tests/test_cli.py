# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

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


async def test_dns_sync_pushes_the_database_lists(admin):
    """install.sh runs `smp dns-sync` so a new/emptied sinkhole volume gets the lists back."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    # entries saved through the API (its own push fails here: no sinkhole in tests)
    await admin.post("/api/v1/dns/blocklist", json={"domain": "games.example"})
    await admin.post("/api/v1/dns/allowlist", json={"domain": "school.example"})
    assert [e["domain"] for e in (await admin.get("/api/v1/dns/blocklist")).json()["items"]] == ["games.example"]

    received: list[tuple[str, str, dict]] = []

    class Sinkhole(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers.get("Authorization", ""), body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Sinkhole)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        r = smp("dns-sync", SMP_DNS_SINKHOLE_URL=url, SMP_DNS_SYNC_TOKEN="sync-secret-123")
        assert r.returncode == 0, r.stderr
        assert "1 blocked and 1 allowed" in r.stdout
    finally:
        srv.shutdown()
    ((path, auth, body),) = received
    assert path == "/api/blocklist/sync" and auth == "Bearer sync-secret-123"
    assert [e["domain"] for e in body["entries"]] == ["games.example"]
    assert [e["domain"] for e in body["allowlist"]] == ["school.example"]

    r = smp("dns-sync", SMP_DNS_SINKHOLE_URL="http://127.0.0.1:9")  # nothing listens there
    assert r.returncode == 1 and "didn't take the lists" in r.stderr
