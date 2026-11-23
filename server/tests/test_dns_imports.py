# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Hosts-file imports as background jobs (smp/modules/dns/imports.py), and the parts of
the DNS module that must stay light with a big blocklist (sync, check, single edits)."""

import asyncio
import json

import pytest

from tests.conftest import login_as

HDR = {"content-type": "text/plain"}


async def wait_for(client, job: dict, *states: str, seconds: float = 30) -> dict:
    """Poll the job until it reaches one of `states` (or fails)."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        r = await client.get(f"/api/v1/dns/blocklist/imports/{job['id']}")
        assert r.status_code == 200, r.text
        job = r.json()
        if job["state"] in states or job["state"] == "failed":
            return job
        assert asyncio.get_running_loop().time() < deadline, job
        await asyncio.sleep(0.02)


async def upload(client, content: bytes) -> dict:
    """Start an import and wait until it's checked."""
    r = await client.post("/api/v1/dns/blocklist/imports", content=content, headers=HDR)
    assert r.status_code == 202, r.text
    job = await wait_for(client, r.json(), "ready")
    assert job["state"] == "ready", job
    return job


async def run_import(client, job: dict) -> dict:
    r = await client.post(f"/api/v1/dns/blocklist/imports/{job['id']}/confirm")
    assert r.status_code == 202, r.text
    done = await wait_for(client, r.json(), "done")
    assert done["state"] == "done", done
    return done


async def domains_of(client, job: dict, kind: str, **page) -> list[str]:
    r = await client.get(f"/api/v1/dns/blocklist/imports/{job['id']}/domains", params={"kind": kind, **page})
    assert r.status_code == 200, r.text
    return r.json()["items"]


@pytest.fixture
def synced(monkeypatch):
    """Captures what would go to the sinkhole (no Go service in tests)."""
    sent: list[dict] = []

    async def fake_sync(org_id):
        from smp.core import db
        from smp.modules.dns import api

        async with db.sessionmaker()() as s:
            sent.append(json.loads((await api.sync_payload(s, org_id)).body))

    monkeypatch.setattr("smp.modules.dns.api.sync_org", fake_sync)
    return sent


def hosts_file(n: int, prefix: str = "ads") -> bytes:
    lines = ["# a hosts file", "127.0.0.1 localhost", "127.0.0.1 localhost.localdomain", "::1 ip6-localhost"]
    lines += [f"0.0.0.0 {prefix}{i}.example" for i in range(n)]
    return ("\n".join(lines) + "\n").encode()


async def test_import_job_from_upload_to_sinkhole(admin, synced):
    await admin.post("/api/v1/dns/blocklist", json={"domain": "ads3.example"})  # already listed
    await admin.post("/api/v1/dns/allowlist", json={"domain": "ads4.example"})  # never blocked
    synced.clear()

    content = hosts_file(5000)
    job = await upload(admin, content)
    assert job["file_bytes"] == len(content) and job["progress"] == 1.0
    assert (job["new"], job["existing"], job["skipped"]) == (4998, 1, 1)  # localhost.localdomain isn't a site
    assert await domains_of(admin, job, "new", limit=3) == ["ads0.example", "ads1.example", "ads2.example"]
    assert await domains_of(admin, job, "new", limit=2, offset=4996) == ["ads4998.example", "ads4999.example"]
    assert await domains_of(admin, job, "existing") == ["ads3.example"]
    assert await domains_of(admin, job, "skipped") == ["ads4.example"]
    assert synced == []  # checking changes nothing

    done = await run_import(admin, job)
    assert done["added"] == 4998 and done["new"] == 4998 and done["sync_ok"] is True
    assert (await admin.get("/api/v1/dns/blocklist", params={"limit": 1})).json()["total"] == 4999
    entry = (await admin.get("/api/v1/dns/blocklist", params={"q": "ads4999."})).json()["items"][0]
    assert entry["addedBy"] == "imported by: admin" and entry["reason"] == "imported"
    assert len(synced) == 1 and len(synced[0]["entries"]) == 4999
    (audit,) = (await admin.get("/api/v1/audit", params={"action": "dns.blocklist.import"})).json()
    assert audit["actor_name"] == "admin"
    assert audit["details"] == {"imported": 4998, "already_listed": 1, "skipped": 1, "addedBy": "imported by: admin"}

    # done: confirming again is refused; the job can be forgotten
    assert (await admin.post(f"/api/v1/dns/blocklist/imports/{job['id']}/confirm")).json()["code"] == "dns.import_not_ready"
    assert (await admin.delete(f"/api/v1/dns/blocklist/imports/{job['id']}")).status_code == 204
    assert (await admin.get(f"/api/v1/dns/blocklist/imports/{job['id']}")).status_code == 404


async def test_a_domain_added_meanwhile_is_fine(admin, synced):
    job = await upload(admin, hosts_file(10))
    await admin.post("/api/v1/dns/blocklist", json={"domain": "ads5.example"})  # between check and confirm
    done = await run_import(admin, job)
    assert done["added"] == 9
    assert (await admin.get("/api/v1/dns/blocklist", params={"limit": 1})).json()["total"] == 10


async def test_jobs_belong_to_who_started_them(app, admin, synced):
    job = await upload(admin, hosts_file(3))
    async with login_as(app, admin, "dns.imp.t", [{"role": "teacher"}]) as teacher:
        for method, path in (("get", ""), ("get", "/domains"), ("post", "/confirm"), ("delete", "")):
            r = await getattr(teacher, method)(f"/api/v1/dns/blocklist/imports/{job['id']}{path}")
            assert r.status_code == 404, (method, path, r.text)
    async with login_as(app, admin, "dns.imp.s", [{"role": "student"}]) as student:
        assert (await student.post("/api/v1/dns/blocklist/imports", content=b"0.0.0.0 x.example\n", headers=HDR)).status_code == 403


async def test_limits_and_cancelling(admin, synced, monkeypatch):
    from smp.modules.dns import imports

    monkeypatch.setattr(imports, "MAX_IMPORT_BYTES", 1024)
    r = await admin.post("/api/v1/dns/blocklist/imports", content=b"0.0.0.0 x.example\n" * 100, headers=HDR)
    assert r.status_code == 413 and r.json()["code"] == "upload.too_large"
    monkeypatch.setattr(imports, "MAX_IMPORT_BYTES", 1024 * 1024)

    job = await upload(admin, hosts_file(3))
    assert (await admin.delete(f"/api/v1/dns/blocklist/imports/{job['id']}")).status_code == 204
    r = await admin.post(f"/api/v1/dns/blocklist/imports/{job['id']}/confirm")
    assert r.status_code == 404  # cancelled: gone
    assert (await admin.get("/api/v1/dns/blocklist", params={"limit": 1})).json()["total"] == 0


async def test_one_import_per_person_and_a_few_per_school(app, admin, synced, monkeypatch):
    """Each held import keeps its whole domain list in memory (imports.py)."""
    from smp.modules.dns import imports

    # A new upload replaces the person's unconfirmed preview...
    first = await upload(admin, hosts_file(2))
    second = await upload(admin, hosts_file(3))
    assert (await admin.get(f"/api/v1/dns/blocklist/imports/{first['id']}")).status_code == 404
    assert second["new"] == 3

    # ...but not one that is still being checked: that one must finish first.
    held = asyncio.Event()

    async def slow_check(job, content):
        await held.wait()

    monkeypatch.setattr(imports, "check", slow_check)
    r = await admin.post("/api/v1/dns/blocklist/imports", content=hosts_file(1), headers=HDR)
    assert r.status_code == 202 and r.json()["state"] == "checking"
    r = await admin.post("/api/v1/dns/blocklist/imports", content=hosts_file(1), headers=HDR)
    assert r.status_code == 429 and r.json()["code"] == "dns.import_in_progress"

    # The school as a whole: at most MAX_JOBS_PER_ORG imports holding domains.
    monkeypatch.setattr(imports, "MAX_JOBS_PER_ORG", 2)
    async with login_as(app, admin, "dns.imp.a", [{"role": "teacher"}]) as a, \
               login_as(app, admin, "dns.imp.b", [{"role": "teacher"}]) as b:
        assert (await a.post("/api/v1/dns/blocklist/imports", content=hosts_file(1), headers=HDR)).status_code == 202
        r = await b.post("/api/v1/dns/blocklist/imports", content=hosts_file(1), headers=HDR)
        assert r.status_code == 429 and r.json()["code"] == "dns.too_many_imports" and r.json()["params"] == {"max": 2}
    held.set()
    app.state.registry.get("dns").imports._jobs.clear()  # the fake checks never finish their jobs


async def test_a_sinkhole_that_is_down_doesnt_lose_the_import(admin, monkeypatch):
    import httpx

    async def down(org_id):
        raise httpx.ConnectError("no sinkhole")

    monkeypatch.setattr("smp.modules.dns.api.sync_org", down)
    done = await run_import(admin, await upload(admin, hosts_file(20)))
    assert done["added"] == 20 and done["sync_ok"] is False  # saved; synced with the next change


async def test_unreadable_upload_fails_the_job_not_the_server(admin, synced, monkeypatch):
    from smp.modules.dns import imports

    def broken(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(imports, "parse_hosts", broken)
    r = await admin.post("/api/v1/dns/blocklist/imports", content=b"0.0.0.0 x.example\n", headers=HDR)
    job = await wait_for(admin, r.json(), "ready")
    assert job["state"] == "failed" and job["error"]["code"] == "dns.import_failed"


async def test_big_lists_stay_light(admin, synced):
    """Adding, checking and listing with thousands of entries: none of them loads the list
    as ORM objects any more (the sync payload is plain columns)."""
    await run_import(admin, await upload(admin, hosts_file(3000, prefix="t")))
    synced.clear()
    r = await admin.post("/api/v1/dns/blocklist", json={"domain": "games.example"})
    assert r.status_code == 201 and len(synced[0]["entries"]) == 3001
    check = (await admin.get("/api/v1/dns/check", params={"domain": "x.y.games.example"})).json()
    assert check == {"domain": "x.y.games.example", "blocked": True, "blocked_by": "games.example", "allowed_by": None}
    assert (await admin.get("/api/v1/dns/check", params={"domain": "t17.example"})).json()["blocked"] is True
    assert (await admin.get("/api/v1/dns/check", params={"domain": "nothing.example"})).json()["blocked"] is False


async def test_block_page_language_setting_goes_to_the_sinkhole(app, admin, synced):
    status = (await admin.get("/api/v1/dns/status")).json()
    assert status["block_page_language"] == "hu"  # nothing chosen: Hungarian

    r = await admin.put("/api/v1/modules/dns/settings", json={"block_page_language": "en"})
    assert r.status_code == 200, r.text
    await app.state.bus.drain()  # the re-sync runs as an event handler, right after the save
    assert synced and synced[-1]["blockPageLanguage"] == "en"
    assert (await admin.get("/api/v1/dns/status")).json()["block_page_language"] == "en"

    assert (await admin.put("/api/v1/modules/dns/settings", json={"block_page_language": "de"})).status_code == 422
    async with login_as(app, admin, "dns.lang.t", [{"role": "teacher"}]) as teacher:
        # teachers see it on the Site blocker page, but it's a school setting (admins)
        assert (await teacher.get("/api/v1/dns/status")).json()["block_page_language"] == "en"
        assert (await teacher.put("/api/v1/modules/dns/settings", json={"block_page_language": "hu"})).status_code == 403
