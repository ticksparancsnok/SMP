# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Buildings and scoped roles in the policy, audit, token and dashboard endpoints:
building-level policy resolution, owner buildings of policies/schedules, who may assign
what to which target, the scoped assignment list, the building-filtered audit log, token
rules for unit managers and the manager's dashboard."""

import uuid
from datetime import timedelta

from sqlalchemy import select

from smp.core import db
from smp.core.db import utcnow
from smp.core.models import Group, GroupBuilding, Organization, RoleAssignment, User
from smp.core.security import hash_password
from tests.conftest import OWN_PW, login_scoped, make_building

API = "/api/v1"


async def new_policy(client, name, *, building_id=None, blocked=(), expect=201):
    body = {"kind": "testpol", "name": name, "data": {"blocked": list(blocked)}}
    if building_id:
        body["building_id"] = building_id
    r = await client.post(f"{API}/policies", json=body)
    assert r.status_code == expect, r.text
    return r.json()


async def assign(client, policy, target_type, target_id=None, *, expect=201, **extra):
    body = {"policy_id": policy["id"], "target_type": target_type, **extra}
    if target_id:
        body["target_id"] = target_id
    r = await client.post(f"{API}/policy-assignments", json=body)
    assert r.status_code == expect, r.text
    return r.json()


async def make_group(name, building_ids=()):
    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization))).scalar_one()
        g = Group(org_id=org.id, name=name)
        s.add(g)
        await s.flush()
        s.add_all(GroupBuilding(org_id=org.id, group_id=g.id, building_id=uuid.UUID(b)) for b in building_ids)
        await s.commit()
        return str(g.id)


async def make_user(username, roles):
    """A user with arbitrary role assignments (no login needed): its id."""
    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization))).scalar_one()
        u = User(
            org_id=org.id,
            username=username,
            password_hash=hash_password(OWN_PW),
            roles=[
                RoleAssignment(
                    org_id=org.id,
                    role=r["role"],
                    room_id=uuid.UUID(r["room_id"]) if r.get("room_id") else None,
                    building_id=uuid.UUID(r["building_id"]) if r.get("building_id") else None,
                )
                for r in roles
            ],
        )
        s.add(u)
        await s.commit()
        return str(u.id)


# --------------------------------------------------------------------------- resolution


async def test_building_target_resolution(admin):
    bid, (room_a,) = await make_building("Main", ["A1"])
    plain = (await admin.post(f"{API}/rooms", json={"name": "Loose"})).json()
    dev = (await admin.post(f"{API}/devices", json={"hostname": "pc-a1", "room_id": room_a})).json()
    loose = (await admin.post(f"{API}/devices", json={"hostname": "pc-loose", "room_id": plain["id"]})).json()
    org_p = await new_policy(admin, "org-default", blocked=["org"])
    bld_p = await new_policy(admin, "building", blocked=["building"])
    room_p = await new_policy(admin, "room", blocked=["room"])

    async def winner(device):
        r = (await admin.get(f"{API}/policies-resolve", params={"device_id": device["id"]})).json()
        return r["policies"]["testpol"]["policy_name"], r["policies"]["testpol"]["source"]["target_type"]

    await assign(admin, org_p, "org")
    assert (await winner(dev))[0] == "org-default"
    await assign(admin, bld_p, "building", bid)
    # Equal priority: the building beats the org, but only for devices in its rooms.
    assert await winner(dev) == ("building", "building")
    assert (await winner(loose))[0] == "org-default"
    # The room beats the building at equal priority.
    await assign(admin, room_p, "room", room_a)
    assert await winner(dev) == ("room", "room")
    # Priority still decides first.
    high = await new_policy(admin, "exam", blocked=["*"])
    await assign(admin, high, "building", bid, priority=50)
    assert (await winner(dev))[0] == "exam"
    # By IP (room subnet) the same building lookup applies.
    r = await admin.patch(f"{API}/rooms/{room_a}", json={"subnets": ["10.7.0.0/24"]})
    assert r.status_code == 200, r.text
    res = (await admin.get(f"{API}/policies-resolve", params={"ip": "10.7.0.5"})).json()
    assert res["policies"]["testpol"]["policy_name"] == "exam"


# --------------------------------------------------------------------------- owner buildings


async def test_manager_policies_and_schedules(app, admin):
    a, _ = await make_building("A", ["A1"])
    b, _ = await make_building("B", ["B1"])
    school = await new_policy(admin, "school")
    pol_b = await new_policy(admin, "b-only", building_id=b)
    sched_school = (await admin.post(f"{API}/schedules", json={"name": "lessons"})).json()
    sched_b = (await admin.post(f"{API}/schedules", json={"name": "b-lessons", "building_id": b})).json()

    async with login_scoped(app, "mgr_a", [{"role": "manager", "building_id": a}]) as m:
        own = await new_policy(m, "a-only", building_id=a, blocked=["x"])
        assert own["building_id"] == a
        await new_policy(m, "school-2", expect=403)  # school-wide: unit level only
        await new_policy(m, "b-2", building_id=b, expect=403)
        await new_policy(m, "dup", building_id=a)
        await new_policy(m, "dup", building_id=a, expect=409)  # same name in the same building
        await new_policy(admin, "dup", building_id=b)  # ... but fine in another one

        # Edit / delete: own only.
        assert (await m.patch(f"{API}/policies/{own['id']}", json={"description": "x"})).status_code == 200
        assert (await m.patch(f"{API}/policies/{school['id']}", json={"description": "x"})).status_code == 403
        assert (await m.patch(f"{API}/policies/{pol_b['id']}", json={"description": "x"})).status_code == 403
        assert (await m.delete(f"{API}/policies/{school['id']}")).status_code == 403
        assert (await m.delete(f"{API}/policies/{pol_b['id']}")).status_code == 403
        # Moving needs both ends: not to B, not to school-wide.
        assert (await m.patch(f"{API}/policies/{own['id']}", json={"building_id": b})).status_code == 403
        assert (await m.patch(f"{API}/policies/{own['id']}", json={"building_id": None})).status_code == 403
        assert (await m.patch(f"{API}/policies/{own['id']}", json={"data": {"blocked": "bad"}})).status_code == 422

        # Reading: school-wide + own building, not B's.
        names = {p["name"] for p in (await m.get(f"{API}/policies")).json()}
        assert names == {"school", "a-only", "dup"}
        assert (await m.get(f"{API}/policies/{school['id']}")).status_code == 200
        assert (await m.get(f"{API}/policies/{pol_b['id']}")).status_code == 404

        # Schedules follow the same rules.
        r = await m.post(f"{API}/schedules", json={"name": "a-lessons", "building_id": a})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        assert (await m.post(f"{API}/schedules", json={"name": "s2"})).status_code == 403
        assert (await m.post(f"{API}/schedules", json={"name": "s3", "building_id": b})).status_code == 403
        assert {s["name"] for s in (await m.get(f"{API}/schedules")).json()} == {"lessons", "a-lessons"}
        win = [{"days": [0], "start": "08:00", "end": "09:00"}]
        # PUT without building_id keeps the owner; an explicit null (school-wide) is refused.
        r = await m.put(f"{API}/schedules/{sid}", json={"name": "a-lessons", "windows": win})
        assert r.status_code == 200 and r.json()["building_id"] == a
        r = await m.put(f"{API}/schedules/{sid}", json={"name": "a-lessons", "windows": win, "building_id": None})
        assert r.status_code == 403
        assert (await m.put(f"{API}/schedules/{sched_school['id']}", json={"name": "lessons"})).status_code == 403
        assert (await m.put(f"{API}/schedules/{sched_b['id']}", json={"name": "b-lessons"})).status_code == 403
        assert (await m.delete(f"{API}/schedules/{sched_b['id']}")).status_code == 403
        assert (await m.delete(f"{API}/schedules/{sid}")).status_code == 204
        assert (await m.delete(f"{API}/policies/{own['id']}")).status_code == 204

    # A building teacher reads (own building + school-wide) but doesn't write.
    async with login_scoped(app, "teach_a", [{"role": "teacher", "building_id": a}]) as t:
        assert {p["name"] for p in (await t.get(f"{API}/policies")).json()} >= {"school", "dup"}
        assert "b-only" not in {p["name"] for p in (await t.get(f"{API}/policies")).json()}
        await new_policy(t, "t", building_id=a, expect=403)

    # Unit manager: school-wide and every building.
    async with login_scoped(app, "unit", [{"role": "unit_manager"}]) as u:
        await new_policy(u, "u-school")
        await new_policy(u, "u-b", building_id=b)
        assert (await u.patch(f"{API}/policies/{pol_b['id']}", json={"building_id": a})).status_code == 200
    # Unknown building: 404.
    await new_policy(admin, "ghost", building_id=str(uuid.uuid4()), expect=404)


# --------------------------------------------------------------------------- assignments


async def test_manager_assignment_rules(app, admin):
    a, (ra,) = await make_building("A", ["A1"])
    b, (rb,) = await make_building("B", ["B1"])
    dev_a = (await admin.post(f"{API}/devices", json={"hostname": "pc-a", "room_id": ra})).json()
    dev_b = (await admin.post(f"{API}/devices", json={"hostname": "pc-b", "room_id": rb})).json()
    dev_free = (await admin.post(f"{API}/devices", json={"hostname": "pc-free"})).json()
    school = await new_policy(admin, "school")
    pol_a = await new_policy(admin, "a-owned", building_id=a)
    pol_b = await new_policy(admin, "b-owned", building_id=b)
    sched_b = (await admin.post(f"{API}/schedules", json={"name": "b-s", "building_id": b})).json()
    org_asg = await assign(admin, school, "org")

    async with login_scoped(app, "mgr_a", [{"role": "manager", "building_id": a}]) as m:
        # School-wide policies go to the manager's own building and rooms, not elsewhere.
        mine = await assign(m, school, "building", a)
        await assign(m, school, "room", ra)
        await assign(m, school, "device", dev_a["id"])
        await assign(m, school, "building", b, expect=403)
        await assign(m, school, "room", rb, expect=403)
        await assign(m, school, "device", dev_b["id"], expect=403)
        await assign(m, school, "device", dev_free["id"], expect=403)  # in no room: unit level
        await assign(m, school, "org", expect=403)
        # Policies of the own building yes, another building's no; same for schedules.
        await assign(m, pol_a, "building", a)
        await assign(m, pol_b, "building", a, expect=403)
        await assign(m, school, "room", ra, schedule_id=sched_b["id"], expect=403)
        # Unknown target: 404 as before.
        await assign(m, school, "room", str(uuid.uuid4()), expect=404)
        # Removing: own scope only.
        assert (await m.delete(f"{API}/policy-assignments/{mine['id']}")).status_code == 204
        assert (await m.delete(f"{API}/policy-assignments/{org_asg['id']}")).status_code == 403
        other = await assign(admin, school, "building", b)
        assert (await m.delete(f"{API}/policy-assignments/{other['id']}")).status_code == 403

    # A building teacher assigns in the building's rooms but can't target the building.
    async with login_scoped(app, "teach_a", [{"role": "teacher", "building_id": a}]) as t:
        await assign(t, school, "room", ra)
        await assign(t, school, "device", dev_a["id"])
        await assign(t, school, "building", a, expect=403)
        await assign(t, school, "room", rb, expect=403)
        await assign(t, pol_a, "room", ra)  # own building's policy
        await assign(t, pol_b, "room", ra, expect=403)

    # A room teacher: school-wide policies only (the legacy exam-mode case).
    async with login_scoped(app, "teach_r", [{"role": "teacher", "room_id": ra}]) as t:
        await assign(t, school, "room", ra)
        await assign(t, pol_a, "room", ra, expect=403)

    # A unit manager may do all of it.
    async with login_scoped(app, "unit", [{"role": "unit_manager"}]) as u:
        await assign(u, school, "org", expect=201)
        await assign(u, pol_b, "building", b)
        await assign(u, school, "building", b)


async def test_manager_group_and_user_targets(app, admin):
    a, (ra,) = await make_building("A", ["A1"])
    b, (rb,) = await make_building("B", ["B1"])
    school = await new_policy(admin, "school")
    g_a = await make_group("9.A", [a])
    g_ab = await make_group("mixed", [a, b])
    g_b = await make_group("9.B", [b])
    g_none = await make_group("everyone")
    u_a = await make_user("stud_a", [{"role": "student", "building_id": a}])
    u_room = await make_user("teach_room_a", [{"role": "teacher", "room_id": ra}])
    u_ab = await make_user("both", [{"role": "teacher", "building_id": a}, {"role": "student", "building_id": b}])
    u_b = await make_user("stud_b", [{"role": "student", "building_id": b}])
    u_org = await make_user("org_user", [{"role": "teacher"}])
    u_mix = await make_user("room_and_org", [{"role": "teacher", "room_id": ra}, {"role": "student"}])
    u_none = await make_user("no_roles", [])

    async with login_scoped(app, "mgr_a", [{"role": "manager", "building_id": a}]) as m:
        await assign(m, school, "group", g_a)
        for g in (g_ab, g_b, g_none):  # not entirely inside the manager's buildings / school-wide
            await assign(m, school, "group", g, expect=403)
        await assign(m, school, "user", u_a)
        await assign(m, school, "user", u_room)  # a role scoped to a room of the building counts
        for u in (u_ab, u_b, u_org, u_mix, u_none):
            await assign(m, school, "user", u, expect=403)
        await assign(m, school, "user", str(uuid.uuid4()), expect=404)

    async with login_scoped(app, "teach_a", [{"role": "teacher", "building_id": a}]) as t:
        await assign(t, school, "group", g_a, expect=403)
        await assign(t, school, "user", u_a, expect=403)

    async with login_scoped(app, "unit", [{"role": "unit_manager"}]) as u:
        await assign(u, school, "group", g_none)
        await assign(u, school, "user", u_ab)


async def test_assignment_listing_is_scoped(app, admin):
    a, (ra,) = await make_building("A", ["A1"])
    b, (rb,) = await make_building("B", ["B1"])
    dev_a = (await admin.post(f"{API}/devices", json={"hostname": "pc-a", "room_id": ra})).json()
    dev_b = (await admin.post(f"{API}/devices", json={"hostname": "pc-b", "room_id": rb})).json()
    g_a, g_b = await make_group("ga", [a]), await make_group("gb", [b])
    u_a = await make_user("ua", [{"role": "student", "building_id": a}])
    u_b = await make_user("ub", [{"role": "student", "building_id": b}])
    u_org = await make_user("uorg", [{"role": "student"}])
    p = await new_policy(admin, "school")
    expected = {}
    for label, tt, tid in [
        ("org", "org", None),
        ("bA", "building", a),
        ("bB", "building", b),
        ("rA", "room", ra),
        ("rB", "room", rb),
        ("dA", "device", dev_a["id"]),
        ("dB", "device", dev_b["id"]),
        ("gA", "group", g_a),
        ("gB", "group", g_b),
        ("uA", "user", u_a),
        ("uB", "user", u_b),
        ("uOrg", "user", u_org),
    ]:
        expected[label] = (await assign(admin, p, tt, tid, label=label))["id"]

    async def labels(client):
        rows = (await client.get(f"{API}/policy-assignments")).json()
        return {r["label"]: r for r in rows}

    everything = set(expected)
    assert set(await labels(admin)) == everything
    async with login_scoped(app, "unit", [{"role": "unit_manager"}]) as u:
        assert set(await labels(u)) == everything
    async with login_scoped(app, "mgr_a", [{"role": "manager", "building_id": a}]) as m:
        seen = await labels(m)
        assert set(seen) == {"org", "bA", "rA", "dA", "gA", "uA"}
        assert seen["bA"]["target_name"] == "A" and seen["rA"]["target_name"] == "A1"
        assert seen["dA"]["target_name"] == "pc-a" and seen["gA"]["target_name"] == "ga"
        assert seen["uA"]["target_name"] == "ua" and seen["org"]["target_name"] is None
        # Filters still work on top of the scope.
        rows = (await m.get(f"{API}/policy-assignments", params={"target_type": "room"})).json()
        assert [r["label"] for r in rows] == ["rA"]
        rows = (await m.get(f"{API}/policy-assignments", params={"target_id": rb})).json()
        assert rows == []
    async with login_scoped(app, "teach_a", [{"role": "teacher", "building_id": a}]) as t:
        assert set(await labels(t)) == {"org", "bA", "rA", "dA", "gA", "uA"}
    # A room teacher keeps the legacy org-wide read.
    async with login_scoped(app, "teach_r", [{"role": "teacher", "room_id": ra}]) as t:
        assert set(await labels(t)) == everything
    # A student has no access at all.
    async with login_scoped(app, "stud", [{"role": "student", "building_id": a}]) as s:
        assert (await s.get(f"{API}/policy-assignments")).status_code == 403
        await assign(s, p, "room", ra, expect=403)


# --------------------------------------------------------------------------- audit


async def test_audit_is_building_scoped(app, admin, anon):
    a, (ra,) = await make_building("A", ["A1"])
    b, (rb,) = await make_building("B", ["B1"])
    await new_policy(admin, "in-a", building_id=a)
    await new_policy(admin, "in-b", building_id=b)
    school = await new_policy(admin, "school-wide")  # org-level entry
    await assign(admin, school, "room", ra)
    await assign(admin, school, "room", rb)
    await assign(admin, school, "org")
    r = await anon.post(f"{API}/auth/login", json={"username": "admin", "password": "wrong"})  # org-level entry
    assert r.status_code == 401

    def policy_names(rows):
        return {e["details"].get("name") for e in rows if e["action"] == "core.policy.create"}

    async def entries(client, **params):
        r = await client.get(f"{API}/audit", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    all_admin = await entries(admin)
    assert policy_names(all_admin) == {"in-a", "in-b", "school-wide"}

    async with login_scoped(app, "mgr_a", [{"role": "manager", "building_id": a}]) as m:
        rows = await entries(m)
        assert policy_names(rows) == {"in-a"}
        # The room assignment in A is visible, the one in B and the org-level ones are not.
        assigns = [e for e in rows if e["action"] == "core.policy.assign"]
        assert [e["details"]["target_id"] for e in assigns] == [ra]
        assert not [e for e in rows if e["action"] == "core.auth.login_failed"]
        assert 0 < len(rows) < len(all_admin)
        # Existing filters and paging still apply.
        only = await entries(m, action="core.policy.", limit=1)
        assert len(only) == 1
        older = await entries(m, action="core.policy.", before_id=only[0]["id"])
        assert older and all(e["id"] < only[0]["id"] for e in older)
        assert {e["id"] for e in await entries(m, target_type="policy")} <= {e["id"] for e in rows}

    # A manager of a building with no entries sees an empty log (not an error).
    c, _ = await make_building("C", ["C1"])
    async with login_scoped(app, "mgr_c", [{"role": "manager", "building_id": c}]) as m2:
        assert await entries(m2) == []

    async with login_scoped(app, "unit", [{"role": "unit_manager"}]) as u:
        rows = await entries(u)
        assert policy_names(rows) == {"in-a", "in-b", "school-wide"}
        assert [e for e in rows if e["action"] == "core.auth.login_failed"]

    # Teachers and students can't read the audit log.
    async with login_scoped(app, "teach_a", [{"role": "teacher", "building_id": a}]) as t:
        assert (await t.get(f"{API}/audit")).status_code == 403
    async with login_scoped(app, "teach_r", [{"role": "teacher", "room_id": ra}]) as t:
        assert (await t.get(f"{API}/audit")).status_code == 403


# --------------------------------------------------------------------------- tokens


async def test_unit_manager_tokens(app, admin):
    a, _ = await make_building("A", ["A1"])
    admin_tok = (await admin.post(f"{API}/tokens", json={"name": "ops", "permissions": ["core.settings.read"]})).json()
    admin_small = (await admin.post(f"{API}/tokens", json={"name": "ro", "permissions": ["core.devices.read"]})).json()

    async with login_scoped(app, "unit", [{"role": "unit_manager"}]) as u:
        r = await u.post(f"{API}/tokens", json={"name": "dns", "permissions": ["core.devices.read", "dns.blocklist.write"]})
        assert r.status_code == 201, r.text
        mine = r.json()
        # A token never holds more than its creator: admin-only permissions are refused.
        r = await u.post(f"{API}/tokens", json={"name": "bad", "permissions": ["core.settings.read"]})
        assert r.status_code == 403
        assert (await u.post(f"{API}/tokens", json={"name": "x", "permissions": ["core.nope"]})).status_code == 422
        # Listing is unchanged (the whole org).
        assert {t["name"] for t in (await u.get(f"{API}/tokens")).json()} == {"ops", "ro", "dns"}
        # Revoking: only tokens within the caller's own permissions.
        assert (await u.delete(f"{API}/tokens/{admin_tok['id']}")).status_code == 403
        assert (await u.delete(f"{API}/tokens/{admin_small['id']}")).status_code == 204
        assert (await u.delete(f"{API}/tokens/{mine['id']}")).status_code == 204

    ops = [t for t in (await admin.get(f"{API}/tokens")).json() if t["name"] == "ops"][0]
    assert ops["revoked_at"] is None
    assert (await admin.delete(f"{API}/tokens/{admin_tok['id']}")).status_code == 204

    # Managers (like teachers) have no token access.
    async with login_scoped(app, "mgr_a", [{"role": "manager", "building_id": a}]) as m:
        assert (await m.get(f"{API}/tokens")).status_code == 403
        r = await m.post(f"{API}/tokens", json={"name": "t", "permissions": []})
        assert r.status_code == 403
        assert (await m.delete(f"{API}/tokens/{admin_small['id']}")).status_code == 403
        # Module settings stay admin-only for every scoped role.
        assert (await m.get(f"{API}/modules/testpol/settings")).status_code == 403
    async with login_scoped(app, "unit2", [{"role": "unit_manager"}]) as u:
        assert (await u.get(f"{API}/modules/testpol/settings")).status_code == 403


# --------------------------------------------------------------------------- dashboard


async def test_dashboard_has_no_other_building_data(app, admin, anon):
    a, (ra,) = await make_building("A", ["A1"])
    b, (rb,) = await make_building("B", ["B1"])
    await admin.post(f"{API}/devices", json={"hostname": "pc-a", "room_id": ra})
    await admin.post(f"{API}/devices", json={"hostname": "pc-b", "room_id": rb})
    await make_user("stud_a", [{"role": "student", "building_id": a}])
    await make_user("teach_ra", [{"role": "teacher", "room_id": ra}])
    for i in range(3):
        await make_user(f"stud_b{i}", [{"role": "student", "building_id": b}])
    await make_user("teach_org", [{"role": "teacher"}])
    exam = await new_policy(admin, "exam")
    await new_policy(admin, "b-policy", building_id=b)
    ends = (utcnow() + timedelta(hours=1)).isoformat()
    await assign(admin, exam, "room", ra, ends_at=ends)
    await assign(admin, exam, "room", rb, ends_at=ends)
    await assign(admin, exam, "building", b, ends_at=ends)
    await assign(admin, exam, "org", ends_at=ends)
    await anon.post(f"{API}/auth/login", json={"username": "admin", "password": "wrong"})

    async def widgets(client):
        return {w["id"]: w for w in (await client.get(f"{API}/dashboard")).json()}

    top = await widgets(admin)
    assert top["core.temporary_policies"]["value"] == 4
    assert top["core.users"]["value"] == 1 + 1 + 1 + 3 + 1
    assert "core.failed_logins" in top

    async with login_scoped(app, "mgr_a", [{"role": "manager", "building_id": a}]) as m:
        w = await widgets(m)
        # Users of building A: itself, the building student and the teacher of room A1.
        assert w["core.users"]["value"] == 3
        assert w["core.users"]["data"] == {"admin": 0, "unit_manager": 0, "manager": 1, "teacher": 1, "student": 1}
        assert "3 users" not in w["core.users"]["hint"] and "manager" in w["core.users"]["hint"]
        assert w["core.rooms"]["value"] == 1 and w["core.devices"]["value"] == 1
        # Temporary policies: the org one and building A's room; B's room and building are hidden.
        assert w["core.temporary_policies"]["value"] == 2
        assert "core.failed_logins" not in w  # org-level entries
        items = w["core.recent_activity"]["items"]
        assert items and not [i for i in items if i["label"] == "core.auth.login_failed"]
        # Exactly what the manager's own audit log shows (the newest entries overall are
        # org-level or B's, and don't appear).
        own_log = [e["action"] for e in (await m.get(f"{API}/audit", params={"limit": 8})).json()]
        assert [i["label"] for i in items] == own_log
        assert [e["action"] for e in (await admin.get(f"{API}/audit", params={"limit": 1})).json()] != own_log[:1]

    async with login_scoped(app, "unit", [{"role": "unit_manager"}]) as u:
        w = await widgets(u)
        assert w["core.users"]["value"] == top["core.users"]["value"] + 2  # + the two users made since
        assert w["core.users"]["data"]["unit_manager"] == 1
        assert w["core.temporary_policies"]["value"] == 4
        assert "core.failed_logins" in w
