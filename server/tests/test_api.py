# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""End-to-end tests through the HTTP API."""

from datetime import timedelta

from sqlalchemy import text

from smp.core import db
from smp.core.db import utcnow
from tests.conftest import login_as


async def test_unauthenticated_and_me(admin, anon):
    assert (await anon.get("/api/v1/rooms")).status_code == 401
    me = (await admin.get("/api/v1/auth/me")).json()
    assert me["username"] == "admin"
    assert "core.users.write" in me["permissions"]
    assert "shortlinks.links.manage_all" in me["permissions"]


async def test_cross_origin_cookie_request_rejected(admin):
    r = await admin.post("/api/v1/rooms", json={"name": "X"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = await admin.post("/api/v1/rooms", json={"name": "X"}, headers={"origin": "http://test"})
    assert r.status_code == 201


async def test_room_scoped_teacher(app, admin):
    lab1 = (await admin.post("/api/v1/rooms", json={"name": "Lab 1", "subnets": ["10.0.1.0/24"]})).json()
    lab2 = (await admin.post("/api/v1/rooms", json={"name": "Lab 2"})).json()
    d1 = (await admin.post("/api/v1/devices", json={"hostname": "pc-01", "room_id": lab1["id"], "mac": "AA-BB-CC-00-11-22"})).json()
    d2 = (await admin.post("/api/v1/devices", json={"hostname": "pc-02", "room_id": lab2["id"]})).json()
    assert d1["mac"] == "aa:bb:cc:00:11:22"

    async with login_as(app, admin, "teacher1", [{"role": "teacher", "room_id": lab1["id"]}]) as teacher:
        rooms = (await teacher.get("/api/v1/rooms")).json()
        assert [r["name"] for r in rooms] == ["Lab 1"]
        devices = (await teacher.get("/api/v1/devices")).json()
        assert [d["hostname"] for d in devices["items"]] == ["pc-01"]
        assert (await teacher.get(f"/api/v1/devices/{d1['id']}")).status_code == 200
        assert (await teacher.get(f"/api/v1/devices/{d2['id']}")).status_code == 403
        # Teachers can't create rooms or users.
        assert (await teacher.post("/api/v1/rooms", json={"name": "Mine"})).status_code == 403
        assert (await teacher.post("/api/v1/users", json={"username": "x"})).status_code == 403
        # Non-room permissions of a room-assigned teacher apply org-wide.
        assert (await teacher.get("/api/v1/users")).status_code == 200
        r = await teacher.post("/api/v1/shortlinks/links", json={"target_url": "https://example.org"})
        assert r.status_code == 201
        me = (await teacher.get("/api/v1/auth/me")).json()
        assert "core.devices.read" not in me["permissions"]
        assert "core.devices.read" in me["room_permissions"][lab1["id"]]


async def test_duplicate_is_conflict(admin):
    assert (await admin.post("/api/v1/rooms", json={"name": "Dup"})).status_code == 201
    assert (await admin.post("/api/v1/rooms", json={"name": "Dup"})).status_code == 409


async def test_policy_resolution_and_exam_mode(app, admin, testpol):
    room = (await admin.post("/api/v1/rooms", json={"name": "Lab 3", "subnets": ["10.0.3.0/24"]})).json()
    other = (await admin.post("/api/v1/rooms", json={"name": "Lab 4"})).json()

    async def policy(name, blocked):
        r = await admin.post("/api/v1/policies", json={"kind": "testpol", "name": name, "data": {"blocked": blocked}})
        assert r.status_code == 201, r.text
        return r.json()

    default = await policy("default", ["adult"])
    exam = await policy("exam", ["*"])

    # Unknown kind / invalid data are rejected by the owning module's schema.
    assert (await admin.post("/api/v1/policies", json={"kind": "nope", "name": "x"})).status_code == 422
    bad = {"kind": "testpol", "name": "bad", "data": {"blocked": "notalist"}}
    assert (await admin.post("/api/v1/policies", json=bad)).status_code == 422

    r = await admin.post("/api/v1/policy-assignments", json={"policy_id": default["id"], "target_type": "org"})
    assert r.status_code == 201, r.text

    resolved = (await admin.get("/api/v1/policies-resolve", params={"ip": "10.0.3.17"})).json()
    assert resolved["room_id"] == room["id"]
    assert resolved["policies"]["testpol"]["policy_name"] == "default"

    # A room-scoped teacher can start exam mode in their room but not in another one.
    async with login_as(app, admin, "teacher2", [{"role": "teacher", "room_id": room["id"]}]) as teacher:
        ends = (utcnow() + timedelta(minutes=45)).isoformat()
        body = {"policy_id": exam["id"], "target_type": "room", "priority": 100, "ends_at": ends, "label": "exam"}
        r = await teacher.post("/api/v1/policy-assignments", json={**body, "target_id": room["id"]})
        assert r.status_code == 201, r.text
        r = await teacher.post("/api/v1/policy-assignments", json={**body, "target_id": other["id"]})
        assert r.status_code == 403
        r = await teacher.post("/api/v1/policy-assignments", json={"policy_id": exam["id"], "target_type": "org"})
        assert r.status_code == 403

    resolved = (await admin.get("/api/v1/policies-resolve", params={"ip": "10.0.3.17"})).json()
    assert resolved["policies"]["testpol"]["policy_name"] == "exam"
    # After the exam ends, the default applies again.
    later = (utcnow() + timedelta(hours=1)).isoformat()
    resolved = (await admin.get("/api/v1/policies-resolve", params={"ip": "10.0.3.17", "at": later})).json()
    assert resolved["policies"]["testpol"]["policy_name"] == "default"
    # Other subnets only get the org default.
    resolved = (await admin.get("/api/v1/policies-resolve", params={"ip": "192.168.1.1"})).json()
    assert resolved["room_id"] is None
    assert resolved["policies"]["testpol"]["policy_name"] == "default"

    # Subscribers were told (after commit) that effective policies changed.
    await app.state.bus.drain()
    assert any(e.subject == "core.policy.changed" for e in testpol.received)


async def test_scheduled_assignment(admin):
    room = (await admin.post("/api/v1/rooms", json={"name": "Lab 5"})).json()
    p = (await admin.post("/api/v1/policies", json={"kind": "testpol", "name": "lessons", "data": {"blocked": ["games"]}})).json()
    s = await admin.post(
        "/api/v1/schedules",
        json={"name": "Mon 1st lesson", "windows": [{"days": [0], "start": "08:00", "end": "08:45"}]},
    )
    assert s.status_code == 201, s.text
    r = await admin.post(
        "/api/v1/policy-assignments",
        json={"policy_id": p["id"], "target_type": "room", "target_id": room["id"], "schedule_id": s.json()["id"]},
    )
    assert r.status_code == 201
    d = (await admin.post("/api/v1/devices", json={"hostname": "pc-50", "room_id": room["id"]})).json()

    mon_0810 = "2026-11-23T07:10:00+00:00"  # Monday 08:10 Budapest
    mon_1000 = "2026-11-23T09:00:00+00:00"
    on = (await admin.get("/api/v1/policies-resolve", params={"device_id": d["id"], "at": mon_0810})).json()
    off = (await admin.get("/api/v1/policies-resolve", params={"device_id": d["id"], "at": mon_1000})).json()
    assert on["policies"]["testpol"]["policy_name"] == "lessons"
    assert "testpol" not in off["policies"]

    bad = {"name": "bad", "windows": [{"days": [7], "start": "08:00", "end": "09:00"}]}
    assert (await admin.post("/api/v1/schedules", json=bad)).status_code == 422


async def test_audit_log_is_append_only(admin):
    await admin.post("/api/v1/rooms", json={"name": "Audited"})
    entries = (await admin.get("/api/v1/audit", params={"action": "core.room."})).json()
    assert entries[0]["action"] == "core.room.create"
    assert entries[0]["actor_name"] == "admin"

    async with db.sessionmaker()() as s:
        try:
            await s.execute(text("UPDATE audit_log SET action = 'tampered'"))
            raise AssertionError("update should have been rejected")
        except Exception as e:  # noqa: BLE001
            assert "append-only" in str(e)


async def test_api_tokens(admin, anon):
    r = await admin.post("/api/v1/tokens", json={"name": "dns", "permissions": ["core.devices.read"]})
    assert r.status_code == 201
    token = r.json()["token"]
    hdr = {"authorization": f"Bearer {token}"}
    assert (await anon.get("/api/v1/devices", headers=hdr)).status_code == 200
    assert (await anon.get("/api/v1/users", headers=hdr)).status_code == 403

    await admin.delete(f"/api/v1/tokens/{r.json()['id']}")
    assert (await anon.get("/api/v1/devices", headers=hdr)).status_code == 401

    r = await admin.post("/api/v1/tokens", json={"name": "x", "permissions": ["core.nonexistent"]})
    assert r.status_code == 422


async def test_role_changes_and_self_protection(app, admin):
    me = (await admin.get("/api/v1/auth/me")).json()
    r = await admin.put(f"/api/v1/users/{me['id']}/roles", json=[{"role": "teacher"}])
    assert r.status_code == 400
    u = (await admin.post("/api/v1/users", json={"username": "t3", "roles": [{"role": "teacher"}]})).json()
    r = await admin.put(f"/api/v1/users/{u['id']}/roles", json=[{"role": "teacher"}, {"role": "student"}])
    assert r.status_code == 200
    assert sorted(x["role"] for x in r.json()["roles"]) == ["student", "teacher"]
    r = await admin.put(f"/api/v1/users/{u['id']}/roles", json=[{"role": "teacher"}])
    assert [x["role"] for x in r.json()["roles"]] == ["teacher"]


async def test_groups(admin):
    a = (await admin.post("/api/v1/users", json={"username": "s1"})).json()
    b = (await admin.post("/api/v1/users", json={"username": "s2"})).json()
    g = (await admin.post("/api/v1/groups", json={"name": "10.B", "kind": "class"})).json()
    r = await admin.put(f"/api/v1/groups/{g['id']}/members", json=[a["id"], b["id"]])
    assert r.status_code == 200 and len(r.json()) == 2
    groups = (await admin.get("/api/v1/groups")).json()
    assert groups[0]["member_count"] == 2


async def test_module_settings(admin):
    mods = {m["name"]: m for m in (await admin.get("/api/v1/modules")).json()}
    assert {"core", "shortlinks"} <= set(mods)
    assert mods["shortlinks"]["settings_schema"]["properties"]["max_links_per_user"]
    assert (await admin.get("/api/v1/modules/shortlinks/settings")).json()["max_links_per_user"] == 200
    r = await admin.put("/api/v1/modules/shortlinks/settings", json={"max_links_per_user": 0})
    assert r.status_code == 422
    r = await admin.put("/api/v1/modules/shortlinks/settings", json={"max_links_per_user": 1})
    assert r.status_code == 200
    assert (await admin.post("/api/v1/shortlinks/links", json={"target_url": "https://a.example"})).status_code == 201
    assert (await admin.post("/api/v1/shortlinks/links", json={"target_url": "https://b.example"})).status_code == 409


async def test_shortlinks(app, admin, anon):
    r = await admin.post("/api/v1/shortlinks/links", json={"target_url": "https://moodle.example/course/1", "code": "info9"})
    assert r.status_code == 201
    link = r.json()
    r = await anon.get("/s/info9")
    assert r.status_code == 302 and r.headers["location"] == "https://moodle.example/course/1"
    assert (await admin.get("/api/v1/shortlinks/links")).json()[0]["clicks"] == 1

    assert (await admin.post("/api/v1/shortlinks/links", json={"target_url": "javascript:alert(1)"})).status_code == 422
    past = (utcnow() - timedelta(minutes=1)).isoformat()
    await admin.post("/api/v1/shortlinks/links", json={"target_url": "https://x.example", "code": "old", "expires_at": past})
    assert (await anon.get("/s/old")).status_code == 404

    async with login_as(app, admin, "teacher4", [{"role": "teacher"}]) as teacher:
        assert (await teacher.get("/api/v1/shortlinks/links")).json() == []
        assert (await teacher.delete(f"/api/v1/shortlinks/links/{link['id']}")).status_code == 404
    async with login_as(app, admin, "student1", [{"role": "student"}]) as student:
        # Students can't make short links: /s/<code> on the school's own domain pointing
        # anywhere they like would be a phishing tool.
        assert (await student.get("/api/v1/shortlinks/links")).status_code == 403
        r = await student.post("/api/v1/shortlinks/links", json={"target_url": "https://x.example/s", "code": "login"})
        assert r.status_code == 403
        assert "shortlinks.links.create" not in (await student.get("/api/v1/auth/me")).json()["permissions"]
        assert not [w for w in (await student.get("/api/v1/dashboard")).json() if w["id"] == "shortlinks.mine"]


async def test_migration_disables_links_of_students(app, admin, anon):
    """0007: links made while students still had the permission stop redirecting."""
    import importlib

    from sqlalchemy import text

    from smp.core import db

    mig = importlib.import_module("smp.migrations.versions.20261122_0007_student_links")
    teacher_link = (await admin.post("/api/v1/shortlinks/links", json={"target_url": "https://ok.example", "code": "tlink"})).json()
    student = (await admin.post("/api/v1/users", json={"username": "old.student", "roles": [{"role": "student"}]})).json()
    async with db.sessionmaker()() as s:  # a link from before the permission was removed
        org_id = (await s.execute(text("SELECT org_id FROM users WHERE id = :u"), {"u": student["id"]})).scalar_one()
        await s.execute(
            text("INSERT INTO shortlinks_links (id, org_id, code, target_url, title, owner_id, disabled, clicks, created_at, updated_at)"
                 " VALUES (gen_random_uuid(), :o, 'slink', 'https://evil.example', '', :u, false, 0, now(), now())"),
            {"o": org_id, "u": student["id"]},
        )
        await s.execute(mig.DISABLE_STUDENT_LINKS)
        await s.commit()
    assert (await anon.get("/s/slink", follow_redirects=False)).status_code == 404
    assert (await anon.get(f"/s/{teacher_link['code']}", follow_redirects=False)).status_code == 302


async def test_dashboard(app, admin):
    lab = (await admin.post("/api/v1/rooms", json={"name": "Lab D"})).json()
    await admin.post("/api/v1/rooms", json={"name": "Lab E"})
    await admin.post("/api/v1/devices", json={"hostname": "pc-d1", "room_id": lab["id"]})
    await admin.post("/api/v1/devices", json={"hostname": "pc-loose"})

    widgets = {w["id"]: w for w in (await admin.get("/api/v1/dashboard")).json()}
    assert widgets["core.rooms"]["value"] == 2
    assert widgets["core.devices"]["value"] == 2
    assert "1 without a room" in widgets["core.devices"]["hint"]
    assert widgets["core.devices"]["data"] == {"online": 0, "unassigned": 1}  # the UI builds its hint from this
    assert widgets["core.users"]["data"] == {"admin": 1, "unit_manager": 0, "manager": 0, "teacher": 0, "student": 0}
    assert widgets["core.users"]["value"] == 1
    assert widgets["core.recent_activity"]["kind"] == "list"
    assert widgets["shortlinks.mine"]["value"] == 0
    # Sorted by order, lists last.
    assert list(widgets)[-1] == "core.recent_activity"

    # A teacher scoped to one room only sees counts for that room and no admin cards.
    async with login_as(app, admin, "teacher_d", [{"role": "teacher", "room_id": lab["id"]}]) as teacher:
        tw = {w["id"]: w for w in (await teacher.get("/api/v1/dashboard")).json()}
        assert tw["core.rooms"]["value"] == 1
        assert tw["core.devices"]["value"] == 1
        assert "core.recent_activity" not in tw and "core.failed_logins" not in tw
        assert "without a room" not in tw["core.devices"]["hint"] and "unassigned" not in tw["core.devices"]["data"]

    rooms = (await admin.get("/api/v1/rooms")).json()
    assert {r["name"]: r["device_count"] for r in rooms} == {"Lab D": 1, "Lab E": 0}
    loose = (await admin.get("/api/v1/devices", params={"unassigned": "true"})).json()
    assert [d["hostname"] for d in loose["items"]] == ["pc-loose"]


async def test_serves_ui(tmp_path, migrated_db):
    import httpx

    from smp.core.config import get_settings
    from smp.main import create_app

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>SMP UI</html>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    settings = get_settings()
    settings.ui_dir = str(tmp_path)
    try:
        ui_app = create_app()
    finally:
        settings.ui_dir = ""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app), base_url="http://test") as c:
        assert "SMP UI" in (await c.get("/rooms")).text  # SPA fallback
        assert "SMP UI" in (await c.get("/")).text
        assert (await c.get("/assets/app.js")).text == "console.log(1)"
        assert (await c.get("/api/v1/nope")).status_code == 404
        assert (await c.get("/healthz")).json()["status"] == "ok"


# --------------------------------------------------------------------------- regressions (review fixes)


async def _assign(admin, target_type, target_id, name):
    p = (await admin.post("/api/v1/policies", json={"kind": "testpol", "name": name})).json()
    r = await admin.post("/api/v1/policy-assignments", json={"policy_id": p["id"], "target_type": target_type, "target_id": target_id})
    assert r.status_code == 201, r.text
    return r.json()


async def test_deleting_targets_removes_their_assignments(admin):
    room = (await admin.post("/api/v1/rooms", json={"name": "Del R"})).json()
    dev = (await admin.post("/api/v1/devices", json={"hostname": "del-pc"})).json()
    grp = (await admin.post("/api/v1/groups", json={"name": "Del G"})).json()
    usr = (await admin.post("/api/v1/users", json={"username": "del.u"})).json()
    await _assign(admin, "room", room["id"], "p-room")
    await _assign(admin, "device", dev["id"], "p-dev")
    await _assign(admin, "group", grp["id"], "p-grp")
    await _assign(admin, "user", usr["id"], "p-usr")
    assert len((await admin.get("/api/v1/policy-assignments")).json()) == 4

    for path in (f"/rooms/{room['id']}", f"/devices/{dev['id']}", f"/groups/{grp['id']}", f"/users/{usr['id']}"):
        assert (await admin.delete("/api/v1" + path)).status_code == 204
    assert (await admin.get("/api/v1/policy-assignments")).json() == []
    entry = (await admin.get("/api/v1/audit", params={"action": "core.room.delete"})).json()[0]
    assert entry["details"]["removed_assignments"] == 1


async def test_orphaned_assignment_can_be_removed_by_admin_only(app, admin, org):
    """Assignments orphaned before the cleanup existed (or by direct DB edits)."""
    import uuid

    from smp.core.models import Policy, PolicyAssignment

    lab = (await admin.post("/api/v1/rooms", json={"name": "Orphan Lab"})).json()
    async with db.sessionmaker()() as s:
        p = Policy(org_id=org.id, kind="testpol", name="orphan")
        s.add(p)
        await s.flush()
        a = PolicyAssignment(org_id=org.id, policy_id=p.id, target_type="room", target_id=uuid.uuid4())
        s.add(a)
        await s.commit()
        aid = a.id

    async with login_as(app, admin, "teacher_o", [{"role": "teacher", "room_id": lab["id"]}]) as teacher:
        assert (await teacher.delete(f"/api/v1/policy-assignments/{aid}")).status_code == 403
    assert (await admin.delete(f"/api/v1/policy-assignments/{aid}")).status_code == 204
    # Creating against a missing target is still a 404.
    r = await admin.post(
        "/api/v1/policy-assignments", json={"policy_id": str(p.id), "target_type": "room", "target_id": str(uuid.uuid4())}
    )
    assert r.status_code == 404


async def test_malformed_schedule_windows_are_422_not_500(admin):
    bad_windows = [
        {"days": [0], "end": "09:00"},  # missing start
        {"days": [0], "start": 800, "end": "09:00"},  # not a string
        {"days": [0], "start": "0800", "end": "09:00"},  # no colon
        {"days": [0], "start": "25:00", "end": "09:00"},  # out of range
        {"days": [True], "start": "08:00", "end": "09:00"},  # bool is not a day
    ]
    for w in bad_windows:
        r = await admin.post("/api/v1/schedules", json={"name": "bad", "windows": [w]})
        assert r.status_code == 422, (w, r.status_code)
    r = await admin.post("/api/v1/schedules", json={"name": "bad", "windows": ["nope"]})
    assert r.status_code == 422


async def test_schedule_in_use_cannot_be_deleted(admin):
    room = (await admin.post("/api/v1/rooms", json={"name": "Sched R"})).json()
    s = (await admin.post("/api/v1/schedules", json={"name": "S", "windows": [{"days": [0], "start": "08:00", "end": "09:00"}]})).json()
    p = (await admin.post("/api/v1/policies", json={"kind": "testpol", "name": "sched-p"})).json()
    a = (await admin.post("/api/v1/policy-assignments", json={
        "policy_id": p["id"], "target_type": "room", "target_id": room["id"], "schedule_id": s["id"]})).json()
    r = await admin.delete(f"/api/v1/schedules/{s['id']}")
    assert r.status_code == 409 and "1 policy assignment" in r.json()["detail"]
    assert r.json()["code"] == "policies.schedule_in_use" and r.json()["params"] == {"count": 1}
    await admin.delete(f"/api/v1/policy-assignments/{a['id']}")
    assert (await admin.delete(f"/api/v1/schedules/{s['id']}")).status_code == 204


async def test_logout_with_expired_or_missing_session(app, admin, anon):
    async with db.sessionmaker()() as s:
        await s.execute(text("UPDATE auth_sessions SET expires_at = now() - interval '1 minute'"))
        await s.commit()
    r = await admin.post("/api/v1/auth/logout")
    assert r.status_code == 204
    assert 'smp_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    assert (await anon.post("/api/v1/auth/logout")).status_code == 204  # no cookie at all
    # Still rejects cross-origin requests.
    anon.cookies.set("smp_session", "x")
    r = await anon.post("/api/v1/auth/logout", headers={"origin": "http://evil.example"})
    assert r.status_code == 403


async def test_policy_changed_emitted_for_resolution_changes(app, admin, testpol):
    r1 = (await admin.post("/api/v1/rooms", json={"name": "Ev 1"})).json()
    r2 = (await admin.post("/api/v1/rooms", json={"name": "Ev 2"})).json()
    d = (await admin.post("/api/v1/devices", json={"hostname": "ev-pc", "room_id": r1["id"]})).json()
    u = (await admin.post("/api/v1/users", json={"username": "ev.u"})).json()
    g = (await admin.post("/api/v1/groups", json={"name": "Ev G"})).json()

    async def changed_after(coro) -> bool:
        await app.state.bus.drain()
        testpol.received.clear()
        await coro
        await app.state.bus.drain()
        return any(e.subject == "core.policy.changed" for e in testpol.received)

    assert await changed_after(admin.patch(f"/api/v1/devices/{d['id']}", json={"room_id": r2["id"]}))
    assert not await changed_after(admin.patch(f"/api/v1/devices/{d['id']}", json={"notes": "x"}))
    assert await changed_after(admin.patch(f"/api/v1/rooms/{r1['id']}", json={"subnets": ["10.9.0.0/24"]}))
    assert not await changed_after(admin.patch(f"/api/v1/rooms/{r1['id']}", json={"description": "B"}))
    assert await changed_after(admin.put(f"/api/v1/groups/{g['id']}/members", json=[u["id"]]))
    assert not await changed_after(admin.put(f"/api/v1/groups/{g['id']}/members", json=[u["id"]]))


async def test_effective_policies_joins_policies_once(admin, org):
    from sqlalchemy import event

    from smp.core.policy import effective_policies

    statements: list[str] = []

    def capture(conn, cursor, statement, *args):
        statements.append(statement)

    async with db.sessionmaker()() as s:
        engine = s.bind.sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            await effective_policies(s, org.id, kinds={"testpol"})
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    sql = next(st for st in statements if "policy_assignments" in st)
    assert sql.count("JOIN policies") == 1, sql



# --------------------------------------------------------------------------- students & class membership


async def test_student_permissions_are_minimal(app, admin):
    async with login_as(app, admin, "student_p", [{"role": "student"}]) as student:
        me = (await student.get("/api/v1/auth/me")).json()
        assert me["permissions"] == []  # students: home page and own account only
        assert me["room_permissions"] == {}
        for path in ("/rooms", "/devices", "/users", "/groups", "/policies", "/audit", "/tokens", "/shortlinks/links"):
            assert (await student.get("/api/v1" + path)).status_code == 403, path
        assert (await student.get("/api/v1/dashboard")).json() == []


async def test_user_class_membership(app, admin, testpol):
    a = (await admin.post("/api/v1/groups", json={"name": "9.A", "kind": "class"})).json()
    b = (await admin.post("/api/v1/groups", json={"name": "Kosárlabda", "kind": "custom"})).json()

    r = await admin.post("/api/v1/users", json={"username": "uj.diak", "roles": [{"role": "student"}], "group_ids": [a["id"]]})
    assert r.status_code == 201, r.text
    u = r.json()
    assert [g["name"] for g in u["groups"]] == ["9.A"]

    await app.state.bus.drain()
    testpol.received.clear()
    r = await admin.put(f"/api/v1/users/{u['id']}/groups", json=[b["id"]])
    assert r.status_code == 200
    assert [g["name"] for g in r.json()["groups"]] == ["Kosárlabda"]
    await app.state.bus.drain()
    subjects = [e.subject for e in testpol.received]
    assert subjects.count("core.group.members_changed") == 2 and "core.policy.changed" in subjects

    # visible from the group side and in filters/lists
    members = (await admin.get(f"/api/v1/groups/{b['id']}/members")).json()
    assert [m["username"] for m in members] == ["uj.diak"]
    listed = (await admin.get("/api/v1/users", params={"group_id": b["id"]})).json()
    assert [x["username"] for x in listed["items"]] == ["uj.diak"]
    assert (await admin.get("/api/v1/users", params={"group_id": a["id"]})).json()["total"] == 0

    assert (await admin.put(f"/api/v1/users/{u['id']}/groups", json=["00000000-0000-0000-0000-000000000000"])).status_code == 422
    entry = (await admin.get("/api/v1/audit", params={"action": "core.user.groups"})).json()[0]
    assert entry["details"]["added"] == [b["id"]] and entry["details"]["removed"] == [a["id"]]

    # teachers can read users (incl. their classes) but not change memberships
    async with login_as(app, admin, "tanar_g", [{"role": "teacher"}]) as teacher:
        assert (await teacher.put(f"/api/v1/users/{u['id']}/groups", json=[])).status_code == 403
        got = (await teacher.get(f"/api/v1/users/{u['id']}")).json()
        assert [g["name"] for g in got["groups"]] == ["Kosárlabda"]
