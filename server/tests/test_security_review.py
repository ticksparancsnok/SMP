"""Fixes from the third security review (attack run, 2026-11)."""

import httpx

from tests.conftest import login_as, make_client


def bearer(app, token: str) -> httpx.AsyncClient:
    c = make_client(app)
    c.headers["authorization"] = f"Bearer {token}"
    return c


# --------------------------------------------------------------------------- 1: tokens die with their user


async def test_deleting_user_deletes_their_tokens_and_children(app, admin):
    async with login_as(app, admin, "leaving.admin", [{"role": "admin"}]) as leaving:
        r = await leaving.post("/api/v1/tokens", json={"name": "parent", "permissions": ["core.tokens.manage", "core.users.read"]})
        parent = r.json()
    async with bearer(app, parent["token"]) as p:  # a token minting a token
        child = (await p.post("/api/v1/tokens", json={"name": "child", "permissions": ["core.users.read"]})).json()
    keep = (await admin.post("/api/v1/tokens", json={"name": "dns-server", "permissions": ["core.users.read"]})).json()

    uid = (await admin.get("/api/v1/users", params={"q": "leaving.admin"})).json()["items"][0]["id"]
    assert (await admin.delete(f"/api/v1/users/{uid}")).status_code == 204

    for gone in (parent, child):
        async with bearer(app, gone["token"]) as c:
            assert (await c.get("/api/v1/users")).status_code == 401, gone["name"]
    async with bearer(app, keep["token"]) as c:  # other people's tokens are untouched
        assert (await c.get("/api/v1/users")).status_code == 200
    assert {t["name"] for t in (await admin.get("/api/v1/tokens")).json()} == {"dns-server"}

    entry = (await admin.get("/api/v1/audit", params={"action": "core.token.delete_with_user"})).json()[0]
    assert entry["target_id"] == uid and entry["details"]["username"] == "leaving.admin"
    assert entry["details"]["count"] == 2
    assert [(t["name"], t["created_by_token"]) for t in entry["details"]["tokens"]] == [("parent", False), ("child", True)]
    # nothing that could be used to authenticate ends up in the log
    text = str(entry)
    for t in (parent, child):
        assert t["token"] not in text and t["prefix"] not in text


async def test_removing_admin_role_deletes_their_tokens(app, admin):
    """The attack from the review: an admin mints a token, then is demoted. The token
    (and any token it minted) must stop working with the demotion itself."""
    async with login_as(app, admin, "demoted.admin", [{"role": "admin"}]) as demoted:
        parent = (await demoted.post("/api/v1/tokens", json={"name": "backup", "permissions": ["core.tokens.manage", "core.users.write"]})).json()
    async with bearer(app, parent["token"]) as p:
        child = (await p.post("/api/v1/tokens", json={"name": "child", "permissions": ["core.users.write"]})).json()
    keep = (await admin.post("/api/v1/tokens", json={"name": "dns-server", "permissions": ["core.users.read"]})).json()
    uid = (await admin.get("/api/v1/users", params={"q": "demoted.admin"})).json()["items"][0]["id"]

    # role edits that keep the admin role leave the tokens alone
    assert (await admin.put(f"/api/v1/users/{uid}/roles", json=[{"role": "admin"}, {"role": "teacher"}])).status_code == 200
    async with bearer(app, parent["token"]) as p:
        assert (await p.get("/api/v1/tokens")).status_code == 200  # still valid

    # admin role removed (the user stays, as a teacher): tokens gone
    assert (await admin.put(f"/api/v1/users/{uid}/roles", json=[{"role": "teacher"}])).status_code == 200
    for gone in (parent, child):
        async with bearer(app, gone["token"]) as c:
            r = await c.put(f"/api/v1/users/{uid}/roles", json=[{"role": "admin"}])  # the re-escalation from the PoC
            assert r.status_code == 401, gone["name"]
    async with bearer(app, keep["token"]) as c:
        assert (await c.get("/api/v1/users")).status_code == 200
    assert {t["name"] for t in (await admin.get("/api/v1/tokens")).json()} == {"dns-server"}

    entry = (await admin.get("/api/v1/audit", params={"action": "core.token.delete_with_admin_role"})).json()[0]
    assert entry["target_id"] == uid and entry["details"]["count"] == 2
    assert [t["name"] for t in entry["details"]["tokens"]] == ["backup", "child"]
    assert parent["token"] not in str(entry) and parent["prefix"] not in str(entry)

    # changing a non-admin's roles never touches tokens (and logs nothing about them)
    assert (await admin.put(f"/api/v1/users/{uid}/roles", json=[{"role": "student"}])).status_code == 200
    assert len((await admin.get("/api/v1/audit", params={"action": "core.token.delete_with_admin_role"})).json()) == 1


async def test_deleting_user_without_tokens_logs_no_token_entry(app, admin):
    u = (await admin.post("/api/v1/users", json={"username": "plain.user"})).json()
    assert (await admin.delete(f"/api/v1/users/{u['id']}")).status_code == 204
    assert (await admin.get("/api/v1/audit", params={"action": "core.token.delete_with_user"})).json() == []


# --------------------------------------------------------------------------- 4/5: DNS allowlist, public suffixes, strict parsing


async def _dns_teacher(app, admin, monkeypatch, name):
    """A teacher client; the sinkhole sync is captured instead of sent (no Go service in tests)."""
    sent: list[dict] = []

    async def fake_sync(ctx):
        from smp.modules.dns import api

        sent.append({
            "entries": [r.domain for r in await api._blocklist(ctx) if not api.is_public_suffix(r.domain)],
            "allowlist": [r.domain for r in await api._allowlist(ctx)],
        })

    monkeypatch.setattr("smp.modules.dns.api.sync_sinkhole", fake_sync)
    return login_as(app, admin, name, [{"role": "teacher"}]), sent


async def test_public_suffixes_can_never_be_blocked_or_allowed(app, admin, monkeypatch):
    teacher, sent = await _dns_teacher(app, admin, monkeypatch, "dns.t1")
    async with teacher as t:
        for domain in ("com", "hu", "co.uk", "CO.HU."):
            for path in ("/api/v1/dns/blocklist", "/api/v1/dns/allowlist"):
                r = await t.post(path, json={"domain": domain})
                assert r.status_code == 422, (path, domain, r.text)
                assert r.json()["code"] in ("dns.public_suffix", "dns.invalid_domain")  # "com"/"hu" fail syntax first
        assert (await t.post("/api/v1/dns/blocklist", json={"domain": "co.uk"})).json()["code"] == "dns.public_suffix"
        for bad in ("a..b", "-x.com", "*.ads.com", "x" * 64 + ".com"):
            assert (await t.post("/api/v1/dns/blocklist", json={"domain": bad})).json()["code"] == "dns.invalid_domain"
        allow = (await t.get("/api/v1/dns/allowlist")).json()
        assert allow["entries"] == [] and allow["public_suffixes"] > 6000 and "tldextract" in allow["public_suffix_source"]
    assert sent == []  # nothing changed, nothing synced


async def test_allowlist_is_checked_before_blocklist(app, admin, monkeypatch):
    teacher, sent = await _dns_teacher(app, admin, monkeypatch, "dns.t2")
    async with teacher as t:
        assert (await t.post("/api/v1/dns/blocklist", json={"domain": "news.example"})).status_code == 201
        r = await t.post("/api/v1/dns/allowlist", json={"domain": "school.example", "reason": "our own site"})
        assert r.status_code == 201 and r.json()["addedBy"] == "dns.t2"
        assert (await t.post("/api/v1/dns/allowlist", json={"domain": "school.example"})).json()["code"] == "dns.allow_exists"

        # blocking something the allowlist covers is refused, with the covering entry named
        r = await t.post("/api/v1/dns/blocklist", json={"domain": "games.school.example"})
        assert r.status_code == 422 and r.json()["code"] == "dns.allowlisted"
        assert r.json()["params"]["allowlist_entry"] == "school.example"

        # an allowlist entry added later overrides an existing block (shown as inactive)
        await t.post("/api/v1/dns/allowlist", json={"domain": "edu.news.example"})
        check = (await t.get("/api/v1/dns/check", params={"domain": "edu.news.example"})).json()
        assert check == {"domain": "edu.news.example", "blocked": False, "blocked_by": None, "allowed_by": "edu.news.example"}
        assert (await t.get("/api/v1/dns/check", params={"domain": "www.news.example"})).json()["blocked_by"] == "news.example"
        await t.post("/api/v1/dns/allowlist", json={"domain": "news.example"})
        (entry,) = (await t.get("/api/v1/dns/blocklist")).json()
        assert entry["domain"] == "news.example" and not entry["active"] and entry["inactiveReason"] == "allowlisted"
        assert (await t.get("/api/v1/dns/check", params={"domain": "www.news.example"})).json()["blocked"] is False

        allow_id = next(e["id"] for e in (await t.get("/api/v1/dns/allowlist")).json()["entries"] if e["domain"] == "news.example")
        assert (await t.delete(f"/api/v1/dns/allowlist/{allow_id}")).status_code == 204
        assert (await t.get("/api/v1/dns/blocklist")).json()[0]["active"] is True
    # every change was pushed to the sinkhole with both lists
    assert sent[-1] == {"entries": ["news.example"], "allowlist": ["edu.news.example", "school.example"]}
    audit = (await admin.get("/api/v1/audit", params={"action": "dns.allowlist."})).json()
    assert {a["action"] for a in audit} == {"dns.allowlist.create", "dns.allowlist.delete"}


async def test_legacy_public_suffix_entry_is_shown_but_never_synced(app, admin, monkeypatch):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from smp.core import db
    from smp.core.models import Organization
    from smp.modules.dns.models import DnsBlocklistEntry

    async with db.sessionmaker()() as s:  # e.g. "co.uk" added before this check existed
        org = (await s.execute(select(Organization))).scalar_one()
        s.add(DnsBlocklistEntry(org_id=org.id, domain="co.uk", include_subdomains=True, reason="", added_by="old",
                                added_at=datetime.now(UTC)))
        await s.commit()
    teacher, sent = await _dns_teacher(app, admin, monkeypatch, "dns.t3")
    async with teacher as t:
        (entry,) = (await t.get("/api/v1/dns/blocklist")).json()
        assert entry["active"] is False and entry["inactiveReason"] == "public_suffix"
        assert (await t.get("/api/v1/dns/check", params={"domain": "bbc.co.uk"})).json()["blocked"] is False
        assert (await t.post("/api/v1/dns/blocklist", json={"domain": "ads.example"})).status_code == 201
    assert sent[-1]["entries"] == ["ads.example"]


async def test_hosts_import_skips_allowlisted_and_malformed(app, admin, monkeypatch):
    teacher, sent = await _dns_teacher(app, admin, monkeypatch, "dns.t4")
    hosts = b"""127.0.0.1 localhost
0.0.0.0 ads.example tracker.example
0.0.0.0 co.uk
0.0.0.0 cdn.school.example
0.0.0.0 a..b -bad.example
0.0.0.0 ads.example
"""
    async with teacher as t:
        await t.post("/api/v1/dns/allowlist", json={"domain": "school.example"})
        await t.post("/api/v1/dns/blocklist", json={"domain": "tracker.example"})
        hdr = {"content-type": "text/plain"}
        pre = (await t.post("/api/v1/dns/blocklist/import-preview", content=hosts, headers=hdr)).json()
        assert pre["domains"] == ["ads.example", "tracker.example"]
        assert pre["skipped"] == ["co.uk", "cdn.school.example"]
        r = await t.post("/api/v1/dns/blocklist/import", content=hosts, headers=hdr)
        assert r.status_code == 201 and r.json()["unique_count"] == 1  # tracker.example already there
        assert sorted(e["domain"] for e in (await t.get("/api/v1/dns/blocklist")).json()) == ["ads.example", "tracker.example"]
        r = await t.post("/api/v1/dns/blocklist/import", content=b"0.0.0.0 co.uk\n127.0.0.1 localhost\n", headers=hdr)
        assert r.status_code == 422 and r.json()["code"] == "dns.no_valid_domains"


# --------------------------------------------------------------------------- 9: API docs for admins only


async def test_api_docs_are_admin_only(app, admin, anon):
    for path in ("/api/docs", "/api/openapi.json"):
        assert (await anon.get(path)).status_code == 401, path
    async with login_as(app, admin, "docs.teacher", [{"role": "teacher"}]) as teacher:
        for path in ("/api/docs", "/api/openapi.json"):
            r = await teacher.get(path)
            assert r.status_code == 403 and r.json()["code"] == "docs.admin_only", path
    # API tokens don't count as admins, whatever permissions they hold
    token = (await admin.post("/api/v1/tokens", json={"name": "all", "permissions": ["core.users.write", "core.settings.write"]})).json()
    async with bearer(app, token["token"]) as t:
        assert (await t.get("/api/openapi.json")).status_code == 403
    r = await admin.get("/api/docs")
    assert r.status_code == 200 and "swagger" in r.text.lower() and "/api/openapi.json" in r.text
    schema = (await admin.get("/api/openapi.json")).json()
    assert schema["info"]["title"] == "SMP" and "/api/v1/users" in schema["paths"]
    assert "/api/docs" not in schema["paths"] and "/api/openapi.json" not in schema["paths"]
