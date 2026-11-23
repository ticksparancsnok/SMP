# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Server-side paging of the DNS blocklist, target names on policy assignments, and
per-user UI preferences (rows per page)."""

from tests.conftest import login_as

# --------------------------------------------------------------------------- DNS blocklist paging


async def test_blocklist_is_paged_and_searched_on_the_server(admin, monkeypatch):
    from tests.test_dns_imports import run_import, upload

    async def no_sync(org_id):  # no Go sinkhole in tests
        return None

    monkeypatch.setattr("smp.modules.dns.api.sync_org", no_sync)
    hosts = "".join(f"0.0.0.0 site{i:03d}.example\n" for i in range(120)).encode()
    assert (await run_import(admin, await upload(admin, hosts)))["added"] == 120
    r = await admin.post("/api/v1/dns/blocklist", json={"domain": "games.test", "reason": "Gaming during lessons"})
    assert r.status_code == 201, r.text

    page = (await admin.get("/api/v1/dns/blocklist", params={"limit": 50, "offset": 100})).json()
    assert page["total"] == 121
    # sorted by domain: games.test first, then site000..site119
    assert [e["domain"] for e in page["items"]][:2] == ["site099.example", "site100.example"]
    assert len(page["items"]) == 21

    # q matches domain, reason and added-by, case-insensitively, and total follows it
    by_domain = (await admin.get("/api/v1/dns/blocklist", params={"q": "SITE11"})).json()
    assert by_domain["total"] == 10 and all(e["domain"].startswith("site11") for e in by_domain["items"])
    by_reason = (await admin.get("/api/v1/dns/blocklist", params={"q": "gaming"})).json()
    assert [e["domain"] for e in by_reason["items"]] == ["games.test"]
    by_who = (await admin.get("/api/v1/dns/blocklist", params={"q": "imported by"})).json()
    assert by_who["total"] == 120

    assert (await admin.get("/api/v1/dns/blocklist", params={"limit": 1001})).status_code == 422
    assert (await admin.get("/api/v1/dns/blocklist", params={"offset": -1})).status_code == 422


# --------------------------------------------------------------------------- assignment target names


async def test_assignments_carry_target_names_the_caller_may_read(app, admin):
    room_a = (await admin.post("/api/v1/rooms", json={"name": "Room A"})).json()
    room_b = (await admin.post("/api/v1/rooms", json={"name": "Room B"})).json()
    dev_a = (await admin.post("/api/v1/devices", json={"hostname": "pc-a1", "room_id": room_a["id"]})).json()
    dev_b = (await admin.post("/api/v1/devices", json={"hostname": "pc-b1", "room_id": room_b["id"]})).json()
    group = (await admin.post("/api/v1/groups", json={"name": "10.B", "kind": "class"})).json()
    user = (await admin.post("/api/v1/users", json={"username": "kiss.anna", "display_name": "Kiss Anna"})).json()
    plain = (await admin.post("/api/v1/users", json={"username": "plain"})).json()
    policy = (await admin.post("/api/v1/policies", json={"kind": "testpol", "name": "p"})).json()

    targets = [
        ("org", None),
        ("room", room_a["id"]),
        ("room", room_b["id"]),
        ("device", dev_a["id"]),
        ("device", dev_b["id"]),
        ("group", group["id"]),
        ("user", user["id"]),
        ("user", plain["id"]),
    ]
    for target_type, target_id in targets:
        body = {"policy_id": policy["id"], "target_type": target_type, "target_id": target_id}
        assert (await admin.post("/api/v1/policy-assignments", json=body)).status_code == 201

    def names(rows):
        return {(a["target_type"], a["target_id"]): a["target_name"] for a in rows}

    seen = names((await admin.get("/api/v1/policy-assignments")).json())
    assert seen == {
        ("org", None): None,
        ("room", room_a["id"]): "Room A",
        ("room", room_b["id"]): "Room B",
        ("device", dev_a["id"]): "pc-a1",
        ("device", dev_b["id"]): "pc-b1",
        ("group", group["id"]): "10.B",
        ("user", user["id"]): "Kiss Anna (kiss.anna)",
        ("user", plain["id"]): "plain",  # display name defaults to the username: no "plain (plain)"
    }

    # A teacher scoped to room A sees names only where the list endpoints would show them.
    async with login_as(app, admin, "room.teacher", [{"role": "teacher", "room_id": room_a["id"]}]) as t:
        me = (await t.get("/api/v1/auth/me")).json()
        seen = names((await t.get("/api/v1/policy-assignments")).json())
        assert seen[("room", room_a["id"])] == "Room A"
        assert seen[("device", dev_a["id"])] == "pc-a1"
        assert seen[("room", room_b["id"])] is None
        assert seen[("device", dev_b["id"])] is None
        for perm, key in (("core.groups.read", ("group", group["id"])), ("core.users.read", ("user", user["id"]))):
            assert (seen[key] is not None) == (perm in me["permissions"])


# --------------------------------------------------------------------------- preferences


async def test_page_sizes_are_saved_per_user(app, admin):
    assert (await admin.get("/api/v1/auth/me")).json()["preferences"] == {"page_size": {}, "locale": None}

    r = await admin.patch("/api/v1/auth/preferences", json={"page_size": {"dns.blocklist": 500, "users": 10}})
    assert r.status_code == 200, r.text
    assert r.json() == {"page_size": {"dns.blocklist": 500, "users": 10}, "locale": None}
    # merged, and null forgets one list's choice
    r = await admin.patch("/api/v1/auth/preferences", json={"page_size": {"users": None, "audit": 100}})
    assert r.json() == {"page_size": {"dns.blocklist": 500, "audit": 100}, "locale": None}
    assert (await admin.get("/api/v1/auth/me")).json()["preferences"]["page_size"] == {"dns.blocklist": 500, "audit": 100}

    # only the sizes the UI offers, sane keys, nothing else
    for bad in (
        {"page_size": {"users": 7}},
        {"page_size": {"Users List!": 50}},
        {"page_size": {"x" * 65: 50}},
        {"theme": "dark"},
        {"page_size": {f"list{i}": 50 for i in range(101)}},
    ):
        assert (await admin.patch("/api/v1/auth/preferences", json=bad)).status_code == 422, bad

    # another user's preferences are their own
    async with login_as(app, admin, "pref.teacher", [{"role": "teacher"}]) as t:
        assert (await t.get("/api/v1/auth/me")).json()["preferences"] == {"page_size": {}, "locale": None}
        assert (await t.patch("/api/v1/auth/preferences", json={"page_size": {"users": 100}})).status_code == 200
    assert (await admin.get("/api/v1/auth/me")).json()["preferences"]["page_size"] == {"dns.blocklist": 500, "audit": 100}


async def test_preferences_total_is_capped(admin):
    for chunk in range(2):
        body = {"page_size": {f"list{chunk}-{i}": 50 for i in range(50)}}
        assert (await admin.patch("/api/v1/auth/preferences", json=body)).status_code == 200
    r = await admin.patch("/api/v1/auth/preferences", json={"page_size": {"one.more": 50}})
    assert r.status_code == 422
    # changing a list that is already saved still works at the cap
    assert (await admin.patch("/api/v1/auth/preferences", json={"page_size": {"list0-0": 100}})).status_code == 200


async def test_language_is_saved_on_the_account(admin):
    r = await admin.patch("/api/v1/auth/preferences", json={"locale": "en", "page_size": {"users": 100}})
    assert r.json() == {"page_size": {"users": 100}, "locale": "en"}
    # fields that are not sent stay as they were
    assert (await admin.patch("/api/v1/auth/preferences", json={"page_size": {"audit": 10}})).json()["locale"] == "en"
    assert (await admin.patch("/api/v1/auth/preferences", json={"locale": "hu"})).json() == {
        "page_size": {"users": 100, "audit": 10},
        "locale": "hu",
    }
    assert (await admin.get("/api/v1/auth/me")).json()["preferences"]["locale"] == "hu"
    # null forgets it; only the UI's languages
    assert (await admin.patch("/api/v1/auth/preferences", json={"locale": None})).json()["locale"] is None
    for bad in ("de", "HU", "", 1):
        assert (await admin.patch("/api/v1/auth/preferences", json={"locale": bad})).status_code == 422, bad


async def test_language_can_be_saved_before_the_forced_password_change(app, admin):
    """The language switch is on the password-change screen too; nothing else opens up."""
    from tests.conftest import INITIAL_PW, make_client

    r = await admin.post("/api/v1/users", json={"username": "new.teacher", "password": INITIAL_PW, "roles": [{"role": "teacher"}]})
    assert r.status_code == 201, r.text
    async with make_client(app) as c:
        r = await c.post("/api/v1/auth/login", json={"username": "new.teacher", "password": INITIAL_PW})
        assert r.json()["must_change_password"] is True
        r = await c.patch("/api/v1/auth/preferences", json={"locale": "en"})
        assert r.status_code == 200, r.text
        assert (await c.get("/api/v1/auth/me")).json()["preferences"]["locale"] == "en"
        assert (await c.get("/api/v1/users")).json()["code"] == "auth.password_change_required"
