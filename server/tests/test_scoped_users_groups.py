# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Users and groups under the scoped roles: a manager works inside their building(s), a
unit manager covers the school but not the admins, an admin everything."""

import io

import httpx
from openpyxl import Workbook
from sqlalchemy import select

from smp.core import db
from smp.core.models import AuditLog
from smp.core.xlsx_users import HEADERS, XLSX_MIME
from tests.conftest import login_scoped, make_building, make_client

U = "/api/v1/users"
G = "/api/v1/groups"
PW = "Tavaszi-Szel-42"


async def mk(admin, username, roles, **extra):
    r = await admin.post(U, json={"username": username, "roles": roles, **extra})
    assert r.status_code == 201, r.text
    return r.json()


async def school(admin):
    """Buildings A (room a1) and B (room b1) with one user in each, a user in both, an
    org-wide student and a room teacher in A."""
    A, (a1,) = await make_building("A", ["a1"])
    B, (b1,) = await make_building("B", ["b1"])
    users = {
        "sA": await mk(admin, "sA", [{"role": "student", "building_id": A}]),
        "sB": await mk(admin, "sB", [{"role": "student", "building_id": B}]),
        "both": await mk(admin, "both", [{"role": "student", "building_id": A}, {"role": "student", "building_id": B}]),
        "org": await mk(admin, "org", [{"role": "student"}]),
        "tRoom": await mk(admin, "tRoom", [{"role": "teacher", "room_id": a1}]),
        "orphan": await mk(admin, "orphan", []),
    }
    return A, B, a1, b1, users


def names(r):
    assert r.status_code == 200, r.text
    return {u["username"] for u in r.json()["items"]}


def xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(list(HEADERS["en"]))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def post_import(client: httpx.AsyncClient, data: bytes, **params):
    return await client.post(f"{U}/import", params=params, content=data, headers={"content-type": XLSX_MIME})


async def audit_buildings(action):
    async with db.sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))).scalars()
        return [sorted(map(str, r.building_ids)) if r.building_ids else None for r in rows]


# --------------------------------------------------------------------------- visibility


async def test_manager_sees_only_users_of_own_building(app, admin):
    A, B, _a1, _b1, users = await school(admin)
    async with login_scoped(app, "mgrA", [{"role": "manager", "building_id": A}]) as m:
        assert names(await m.get(U)) == {"sA", "both", "tRoom", "mgrA"}  # a room of A counts as A
        assert names(await m.get(U, params={"role": "student"})) == {"sA", "both"}
        assert names(await m.get(U, params={"role": "manager"})) == {"mgrA"}
        assert (await m.get(f"{U}/{users['sA']['id']}")).status_code == 200
        for hidden in ("sB", "org", "orphan"):
            assert (await m.get(f"{U}/{users[hidden]['id']}")).status_code == 404, hidden
        exported = await m.get(f"{U}/export.xlsx")
        assert exported.status_code == 200
        assert (await admin.get(U, params={"role": "manager"})).json()["total"] == 1  # filter takes the new roles


async def test_building_teacher_sees_only_own_building(app, admin):
    A, _B, _a1, _b1, _users = await school(admin)
    await admin.post(G, json={"name": "classA", "building_ids": [A]})
    await admin.post(G, json={"name": "classWide"})
    async with login_scoped(app, "tchA", [{"role": "teacher", "building_id": A}]) as t:
        assert names(await t.get(U)) == {"sA", "both", "tRoom", "tchA"}
        assert [g["name"] for g in (await t.get(G)).json()] == ["classA"]
        assert (await t.post(U, json={"username": "x"})).status_code == 403
        assert (await t.post(G, json={"name": "nope"})).status_code == 403


# --------------------------------------------------------------------------- granting


async def test_manager_creates_users_in_own_building(app, admin):
    A, B, a1, b1, _ = await school(admin)
    async with login_scoped(app, "mgrA", [{"role": "manager", "building_id": A}]) as m:
        # no scope given: the role goes to the manager's building
        r = await m.post(U, json={"username": "new.student", "roles": [{"role": "student"}]})
        assert r.status_code == 201, r.text
        assert r.json()["roles"] == [{"role": "student", "room_id": None, "building_id": A}]
        assert r.json()["one_time_password"] and r.json()["can_edit"] is True
        r = await m.post(U, json={"username": "new.teacher", "roles": [{"role": "teacher", "room_id": a1}]})
        assert r.status_code == 201, r.text
        assert (await m.post(U, json={"username": "t2", "roles": [{"role": "teacher", "building_id": A}]})).status_code == 201
        assert (await m.get(f"{U}/{r.json()['id']}")).status_code == 200

        # not above their level, not outside their building, never org-wide
        for role in ("manager", "unit_manager", "admin"):
            r = await m.post(U, json={"username": f"bad.{role}", "roles": [{"role": role, "building_id": A}]})
            assert r.status_code == 403 and r.json()["code"] == "users.role_not_grantable", role
        r = await m.post(U, json={"username": "bad1", "roles": [{"role": "student", "building_id": B}]})
        assert r.status_code == 403 and r.json()["code"] == "users.out_of_scope"
        r = await m.post(U, json={"username": "bad2", "roles": [{"role": "teacher", "room_id": b1}]})
        assert r.status_code == 403 and r.json()["code"] == "users.out_of_scope"
        r = await m.post(U, json={"username": "bad3"})  # would be an orphan nobody could reach
        assert r.status_code == 422 and r.json()["code"] == "users.role_required"
        assert (await m.post(U, json={"username": "bad4", "roles": [{"role": "student", "room_id": a1}]})).status_code == 422
        # nothing half-created
        assert names(await admin.get(U, params={"q": "bad"})) == set()
    created = await audit_buildings("core.user.create")
    # the admin's school (sA, sB, both, org, tRoom, orphan), then the manager's three
    assert created == [[A], [B], sorted([A, B]), None, [A], None, [A], [A], [A]]


async def test_manager_of_two_buildings_expands_unscoped_roles(app, admin):
    A, B, *_ = await school(admin)
    async with login_scoped(app, "mgrAB", [{"role": "manager", "building_id": A}, {"role": "manager", "building_id": B}]) as m:
        r = await m.post(U, json={"username": "everywhere", "roles": [{"role": "student"}]})
        assert {x["building_id"] for x in r.json()["roles"]} == {A, B} and len(r.json()["roles"]) == 2
        assert {"sA", "sB", "both"} <= names(await m.get(U))


async def test_role_scope_rules(app, admin):
    A, _B, a1, *_ = await school(admin)
    both = await admin.post(U, json={"username": "x1", "roles": [{"role": "student", "room_id": a1, "building_id": A}]})
    assert both.status_code == 422  # room and building are mutually exclusive
    for role, scope in [
        ({"role": "admin", "building_id": A}, "building"),
        ({"role": "manager"}, "org"),  # a manager needs a building
        ({"role": "unit_manager", "room_id": a1}, "room"),
        ({"role": "student", "room_id": a1}, "room"),
    ]:
        r = await admin.post(U, json={"username": "x2", "roles": [role]})
        assert r.status_code == 422 and r.json()["code"] == "users.scope_not_allowed", role
        assert r.json()["params"]["scope"] == scope
    r = await admin.post(U, json={"username": "x3", "roles": [{"role": "admin", "room_id": a1}]})
    assert r.json()["code"] == "users.admin_not_room_scoped"
    assert (await admin.post(U, json={"username": "x4", "roles": [{"role": "manager", "building_id": A}]})).status_code == 201


# --------------------------------------------------------------------------- editing


async def test_manager_edits_only_what_is_wholly_theirs(app, admin):
    A, B, *_, users = await school(admin)
    peer = await mk(admin, "peer", [{"role": "manager", "building_id": A}])
    async with login_scoped(app, "mgrA", [{"role": "manager", "building_id": A}]) as m:
        # visible but spanning another building / a peer manager / a room teacher outside: not editable
        for name in ("both", "peer"):
            uid = (users.get(name) or peer)["id"]
            assert (await m.patch(f"{U}/{uid}", json={"display_name": "x"})).status_code == 403, name
            assert (await m.put(f"{U}/{uid}/roles", json=[{"role": "student", "building_id": A}])).status_code == 403
            assert (await m.delete(f"{U}/{uid}")).status_code == 403
            assert (await m.post(f"{U}/{uid}/password", json={})).status_code == 403
            assert (await m.put(f"{U}/{uid}/groups", json=[])).status_code == 403
        listed = {u["username"]: u for u in (await m.get(U)).json()["items"]}
        assert listed["sA"]["can_edit"] and not listed["both"]["can_edit"] and not listed["peer"]["can_edit"]
        # invisible: not even a 403
        for name in ("sB", "org", "orphan"):
            assert (await m.patch(f"{U}/{users[name]['id']}", json={"display_name": "x"})).status_code == 404

        uid = users["sA"]["id"]
        assert (await m.patch(f"{U}/{uid}", json={"display_name": "Ann A."})).json()["display_name"] == "Ann A."
        assert (await m.post(f"{U}/{uid}/password", json={"password": PW})).status_code == 200
        r = await m.post(f"{U}/{uid}/password", json={})
        assert r.status_code == 200 and r.json()["one_time_password"]
        # roles: teacher inside the building yes; manager or another building no
        r = await m.put(f"{U}/{uid}/roles", json=[{"role": "teacher", "building_id": A}])
        assert r.status_code == 200 and r.json()["roles"][0]["role"] == "teacher"
        for bad in ([{"role": "manager", "building_id": A}], [{"role": "student", "building_id": B}], [{"role": "student", "building_id": A}, {"role": "admin"}]):
            assert (await m.put(f"{U}/{uid}/roles", json=bad)).status_code == 403
        # login slips: only users of their building
        slips = await m.get(f"{U}/credentials.xlsx")
        assert slips.status_code == 200
        assert (await m.delete(f"{U}/{uid}")).status_code == 204
        # a manager can't promote themselves or touch their own roles
        me = next(u for u in (await m.get(U)).json()["items"] if u["username"] == "mgrA")
        assert (await m.put(f"{U}/{me['id']}/roles", json=[{"role": "student", "building_id": A}])).status_code in (400, 403)
    # audit: the entries about building-A users carry A (and B for the one in both)
    assert await audit_buildings("core.user.delete") == [[A]]
    assert await audit_buildings("core.user.roles") == [[A]]  # the manager's one change


async def test_manager_credentials_slips_cover_only_own_building(app, admin):
    A, B, *_ = await school(admin)  # every user made by the admin has a one-time password
    async with login_scoped(app, "mgrA", [{"role": "manager", "building_id": A}]) as m:
        from openpyxl import load_workbook

        ws = load_workbook(io.BytesIO((await m.get(f"{U}/credentials.xlsx")).content)).active
        got = {row[1].value for row in list(ws.iter_rows())[1:]}
        assert got == {"sA", "tRoom"}  # "both" spans B too, "peer"-like users are not theirs


async def test_unit_manager_and_admin_levels(app, admin):
    A, B, *_ = await school(admin)
    async with login_scoped(app, "unit", [{"role": "unit_manager"}]) as um:
        assert (await um.get(U)).json()["total"] >= 6  # the whole school
        r = await um.post(U, json={"username": "m1", "roles": [{"role": "manager", "building_id": A}]})
        assert r.status_code == 201 and r.json()["roles"][0]["building_id"] == A
        assert (await um.post(U, json={"username": "s", "roles": [{"role": "student"}]})).status_code == 201
        for role in ("admin", "unit_manager"):
            r = await um.post(U, json={"username": f"bad.{role}", "roles": [{"role": role}]})
            assert r.status_code == 403 and r.json()["code"] == "users.role_not_grantable"
        # no editing of the people above them ...
        admin_id = next(u["id"] for u in (await um.get(U, params={"role": "admin"})).json()["items"])
        other_um = await mk(admin, "unit2", [{"role": "unit_manager"}])
        for target in (admin_id, other_um["id"]):
            assert (await um.patch(f"{U}/{target}", json={"display_name": "x"})).status_code == 403
            assert (await um.delete(f"{U}/{target}")).status_code == 403
            assert (await um.post(f"{U}/{target}/password", json={})).status_code == 403
            assert (await um.put(f"{U}/{target}/roles", json=[{"role": "student"}])).status_code == 403
        # ... but anyone below, in any building, or with none
        for name in ("both", "orphan", "m1"):
            uid = (await um.get(U, params={"q": name})).json()["items"][0]["id"]
            assert (await um.patch(f"{U}/{uid}", json={"display_name": "ok"})).status_code == 200, name
        # and not themselves
        me = (await um.get(U, params={"q": "unit"})).json()["items"][0]
        assert (await um.put(f"{U}/{me['id']}/roles", json=[{"role": "admin"}])).status_code in (400, 403)
    # the admin does everything
    assert (await admin.post(U, json={"username": "a2", "roles": [{"role": "admin"}]})).status_code == 201
    assert (await admin.post(U, json={"username": "u3", "roles": [{"role": "unit_manager"}]})).status_code == 201
    assert (await admin.patch(f"{U}/{other_um['id']}", json={"display_name": "ok"})).status_code == 200
    # admins can move users between buildings
    uid = (await admin.get(U, params={"q": "sA"})).json()["items"][0]["id"]
    r = await admin.put(f"{U}/{uid}/roles", json=[{"role": "student", "building_id": B}])
    assert r.status_code == 200 and r.json()["roles"][0]["building_id"] == B
    assert await audit_buildings("core.user.roles") == [sorted([A, B])]  # before and after


async def test_cannot_remove_own_admin_or_change_own_roles(app, admin):
    me = next(u for u in (await admin.get(U, params={"role": "admin"})).json()["items"])
    r = await admin.put(f"{U}/{me['id']}/roles", json=[{"role": "teacher"}])
    assert r.status_code == 400 and r.json()["code"] == "users.cannot_remove_own_admin"
    assert (await admin.put(f"{U}/{me['id']}/roles", json=[{"role": "admin"}, {"role": "teacher"}])).status_code == 200


# --------------------------------------------------------------------------- tokens


async def test_tokens_die_when_a_unit_manager_is_demoted(app, admin):
    A, *_ = await school(admin)
    um = await mk(admin, "um.tok", [{"role": "unit_manager"}])
    async with login_scoped(app, "um.live", [{"role": "unit_manager"}]) as live:
        token = (await live.post("/api/v1/tokens", json={"name": "t", "permissions": ["core.users.read"]})).json()
    umc = make_client(app)
    async with umc:
        r = await umc.post("/api/v1/auth/login", json={"username": "um.tok", "password": um["one_time_password"]})
        assert r.status_code == 200
        await umc.post("/api/v1/auth/password", json={"current_password": um["one_time_password"], "new_password": "sajat-Jelszo-88"})
        mine = (await umc.post("/api/v1/tokens", json={"name": "mine", "permissions": ["core.users.read"]})).json()

    def bearer(tok):
        c = make_client(app)
        c.headers["authorization"] = f"Bearer {tok}"
        return c

    # keeping a role that can manage tokens leaves them alone
    assert (await admin.put(f"{U}/{um['id']}/roles", json=[{"role": "unit_manager"}, {"role": "teacher"}])).status_code == 200
    async with bearer(mine["token"]) as c:
        assert (await c.get(U)).status_code == 200
    # a manager can't mint tokens: the unit manager's die with the role
    assert (await admin.put(f"{U}/{um['id']}/roles", json=[{"role": "manager", "building_id": A}])).status_code == 200
    async with bearer(mine["token"]) as c:
        assert (await c.get(U)).status_code == 401
    async with bearer(token["token"]) as c:  # somebody else's stays
        assert (await c.get(U)).status_code == 200
    assert len(await audit_buildings("core.token.delete_with_admin_role")) == 1


async def test_tokens_die_when_an_admin_is_lowered_to_unit_manager(app, admin):
    """A unit manager still manages tokens, but an admin's token may hold what a unit
    manager doesn't (module settings): lowering the role drops them; raising keeps them."""
    async with login_scoped(app, "adm.low", [{"role": "admin"}]) as a:
        uid = (await a.get("/api/v1/auth/me")).json()["id"]
        tok = (await a.post("/api/v1/tokens", json={"name": "cfg", "permissions": ["core.settings.write"]})).json()
    async with login_scoped(app, "um.up", [{"role": "unit_manager"}]) as u:
        up_id = (await u.get("/api/v1/auth/me")).json()["id"]
        up_tok = (await u.post("/api/v1/tokens", json={"name": "r", "permissions": ["core.users.read"]})).json()

    def bearer(t):
        c = make_client(app)
        c.headers["authorization"] = f"Bearer {t}"
        return c

    assert (await admin.put(f"{U}/{up_id}/roles", json=[{"role": "admin"}])).status_code == 200
    async with bearer(up_tok["token"]) as c:  # promoted: nothing lost, the token stays
        assert (await c.get(U)).status_code == 200
    assert (await admin.put(f"{U}/{uid}/roles", json=[{"role": "unit_manager"}])).status_code == 200
    async with bearer(tok["token"]) as c:
        assert (await c.get("/api/v1/modules/core/settings")).status_code == 401


# --------------------------------------------------------------------------- import


async def test_manager_import_goes_to_own_building(app, admin):
    A, B, *_ = await school(admin)
    await admin.post(G, json={"name": "9.A", "kind": "class", "building_ids": [B]})
    async with login_scoped(app, "mgrA", [{"role": "manager", "building_id": A}]) as m:
        data = xlsx([("Kiss Péter", "kiss.peter", "10.A"), ("Nagy Anna", "nagy.anna", "10.A")])
        assert (await post_import(m, data, building_ids=B)).status_code == 403  # another building
        r = await post_import(m, data)
        assert r.status_code == 200 and r.json()["created"] == ["kiss.peter", "nagy.anna"], r.text
        users = {u["username"]: u for u in (await m.get(U, params={"role": "student"})).json()["items"]}
        assert users["kiss.peter"]["roles"] == [{"role": "student", "room_id": None, "building_id": A}]
        assert users["kiss.peter"]["groups"][0]["name"] == "10.A"
        # the class the file created belongs to the manager's building ...
        assert [g["building_ids"] for g in (await m.get(G)).json()] == [[A]]
        # ... and one that lives elsewhere is refused (nothing written)
        r = await post_import(m, xlsx([("Új Diák", "uj.diak", "9.A")]))
        assert r.status_code == 422 and r.json()["detail"]["errors"][0]["code"] == "users.import_group_out_of_scope"
        assert names(await admin.get(U, params={"q": "uj.diak"})) == set()
    assert await audit_buildings("core.user.import") == [[A]]


async def test_manager_import_may_pick_a_subset_of_their_buildings(app, admin):
    A, B, *_ = await school(admin)
    async with login_scoped(app, "mgrAB", [{"role": "manager", "building_id": A}, {"role": "manager", "building_id": B}]) as m:
        r = await post_import(m, xlsx([("Egy", "egy", "")]), building_ids=B)
        assert r.status_code == 200
        r = await post_import(m, xlsx([("Ketto", "ketto", "")]))
        assert r.status_code == 200
        u = {x["username"]: x for x in (await m.get(U, params={"role": "student"})).json()["items"]}
        assert {x["building_id"] for x in u["egy"]["roles"]} == {B}
        assert {x["building_id"] for x in u["ketto"]["roles"]} == {A, B}


async def test_admin_import_with_building_ids(app, admin):
    A, B, *_ = await school(admin)
    r = await post_import(admin, xlsx([("Kiss Péter", "kiss.peter", "7.A")]), building_ids=[A, B])
    assert r.status_code == 200, r.text
    r = await post_import(admin, xlsx([("Nagy Anna", "nagy.anna", "7.A")]))  # none: school-wide
    assert r.status_code == 200
    users = {u["username"]: u for u in (await admin.get(U, params={"role": "student"})).json()["items"]}
    assert {x["building_id"] for x in users["kiss.peter"]["roles"]} == {A, B}
    assert users["nagy.anna"]["roles"] == [{"role": "student", "room_id": None, "building_id": None}]
    group = next(g for g in (await admin.get(G)).json() if g["name"] == "7.A")
    assert sorted(group["building_ids"]) == sorted([A, B])  # made by the first import
    r = await post_import(admin, xlsx([("X", "x", "")]), building_ids="00000000-0000-0000-0000-000000000000")
    assert r.status_code == 422


# --------------------------------------------------------------------------- groups


async def test_groups_have_several_buildings(app, admin):
    A, B, *_ = await school(admin)
    g = (await admin.post(G, json={"name": "both", "kind": "class", "building_ids": [A, B]})).json()
    assert sorted(g["building_ids"]) == sorted([A, B])
    wide = (await admin.post(G, json={"name": "wide"})).json()
    assert wide["building_ids"] == []
    r = await admin.patch(f"{G}/{g['id']}", json={"building_ids": [B]})
    assert r.status_code == 200 and r.json()["building_ids"] == [B]
    assert (await admin.patch(f"{G}/{g['id']}", json={"name": "renamed"})).json()["building_ids"] == [B]  # kept
    assert (await admin.patch(f"{G}/{g['id']}", json={"building_ids": []})).json()["building_ids"] == []
    assert (await admin.post(G, json={"name": "ghost", "building_ids": ["00000000-0000-0000-0000-000000000000"]})).status_code == 422
    assert [e["details"].get("building_ids") for e in (await admin.get("/api/v1/audit", params={"action": "core.group.update"})).json()][0] == []


async def test_manager_groups(app, admin):
    A, B, *_ = await school(admin)
    ga = (await admin.post(G, json={"name": "ga", "kind": "class", "building_ids": [A]})).json()
    gab = (await admin.post(G, json={"name": "gab", "building_ids": [A, B]})).json()
    gb = (await admin.post(G, json={"name": "gb", "building_ids": [B]})).json()
    wide = (await admin.post(G, json={"name": "wide"})).json()
    async with login_scoped(app, "mgrA", [{"role": "manager", "building_id": A}]) as m:
        assert {g["name"] for g in (await m.get(G)).json()} == {"ga", "gab"}
        for hidden in (gb, wide):
            assert (await m.get(f"{G}/{hidden['id']}")).status_code == 404
            assert (await m.patch(f"{G}/{hidden['id']}", json={"name": "x"})).status_code == 404
        # visible but only partly theirs: read, never write
        assert (await m.get(f"{G}/{gab['id']}")).status_code == 200
        assert (await m.patch(f"{G}/{gab['id']}", json={"name": "x"})).status_code == 403
        assert (await m.delete(f"{G}/{gab['id']}")).status_code == 403
        assert (await m.put(f"{G}/{gab['id']}/members", json=[])).status_code == 403
        assert (await m.put(f"{G}/{gab['id']}/teachers", json=[])).status_code == 403
        # creation: default = their buildings; must be a non-empty subset
        mine = (await m.post(G, json={"name": "mine", "kind": "class"}))
        assert mine.status_code == 201 and mine.json()["building_ids"] == [A]
        for bad in ([B], [A, B], []):
            r = await m.post(G, json={"name": "bad", "building_ids": bad})
            assert r.status_code == 403 and r.json()["code"] == "groups.out_of_scope", bad
        assert (await m.patch(f"{G}/{ga['id']}", json={"name": "ga2"})).status_code == 200
        assert (await m.patch(f"{G}/{ga['id']}", json={"building_ids": [A, B]})).status_code == 403
        assert (await m.patch(f"{G}/{ga['id']}", json={"building_ids": []})).status_code == 403
        # members: users they can see only; the ones they can't see stay
        sA = (await admin.get(U, params={"q": "sA"})).json()["items"][0]["id"]
        sB = (await admin.get(U, params={"q": "sB"})).json()["items"][0]["id"]
        r = await m.put(f"{G}/{ga['id']}/members", json=[sA, sB])
        assert r.status_code == 422
        assert (await m.put(f"{G}/{ga['id']}/members", json=[sA])).status_code == 200
        await admin.put(f"{G}/{ga['id']}/members", json=[sA, sB])
        assert [u["username"] for u in (await m.get(f"{G}/{ga['id']}/members")).json()] == ["sA"]  # sB is hidden ...
        assert (await m.put(f"{G}/{ga['id']}/members", json=[])).status_code == 200
        assert [u["username"] for u in (await admin.get(f"{G}/{ga['id']}/members")).json()] == ["sB"]  # ... and kept
        # teachers: a manager (and a unit manager) qualify, a student doesn't
        peer = await mk(admin, "peer", [{"role": "manager", "building_id": A}])
        r = await m.put(f"{G}/{ga['id']}/teachers", json=[peer["id"]])
        assert r.status_code == 200 and [t["username"] for t in r.json()] == ["peer"]
        assert (await m.put(f"{G}/{ga['id']}/teachers", json=[sA])).status_code == 422
        assert (await m.delete(f"{G}/{mine.json()['id']}")).status_code == 204
    um = await mk(admin, "um.t", [{"role": "unit_manager"}])
    r = await admin.put(f"{G}/{gab['id']}/teachers", json=[um["id"]])
    assert r.status_code == 200 and [t["username"] for t in r.json()] == ["um.t"]
    # group changes are audited for the group's buildings
    assert await audit_buildings("core.group.update") == [[A]]


async def test_manager_adds_users_to_groups_through_the_user(app, admin):
    A, B, *_ = await school(admin)
    ga = (await admin.post(G, json={"name": "ga", "kind": "class", "building_ids": [A]})).json()
    gb = (await admin.post(G, json={"name": "gb", "building_ids": [B]})).json()
    async with login_scoped(app, "mgrA", [{"role": "manager", "building_id": A}]) as m:
        r = await m.post(U, json={"username": "pupil", "roles": [{"role": "student"}], "group_ids": [ga["id"]]})
        assert r.status_code == 201, r.text
        assert [g["name"] for g in r.json()["groups"]] == ["ga"]
        r = await m.post(U, json={"username": "pupil2", "roles": [{"role": "student"}], "group_ids": [gb["id"]]})
        assert r.status_code == 403 and r.json()["code"] == "groups.out_of_scope"
        uid = (await m.get(U, params={"q": "sA"})).json()["items"][0]["id"]
        assert (await m.put(f"{U}/{uid}/groups", json=[ga["id"]])).status_code == 200
        assert (await m.put(f"{U}/{uid}/groups", json=[ga["id"], gb["id"]])).status_code == 403


# --------------------------------------------------------------------------- nothing leaks across buildings


async def test_limited_reader_sees_only_own_groups_of_a_user(app, admin):
    A, B, a1, *_ , users = await school(admin)
    ga = (await admin.post(G, json={"name": "ga", "building_ids": [A]})).json()
    gb = (await admin.post(G, json={"name": "gb", "building_ids": [B]})).json()
    gab = (await admin.post(G, json={"name": "gab", "building_ids": [A, B]})).json()
    wide = (await admin.post(G, json={"name": "wide"})).json()
    uid = users["sA"]["id"]
    for g in (ga, gb, gab, wide):
        assert (await admin.put(f"{G}/{g['id']}/members", json=[uid])).status_code == 200
    everything = ["ga", "gab", "gb", "wide"]
    got = lambda r: sorted(g["name"] for g in r.json()["groups"])  # noqa: E731
    assert got(await admin.get(f"{U}/{uid}")) == everything
    async with login_scoped(app, "mgrA", [{"role": "manager", "building_id": A}]) as m:
        assert got(await m.get(f"{U}/{uid}")) == ["ga", "gab"]
        listed = {u["username"]: u for u in (await m.get(U)).json()["items"]}
        assert sorted(g["name"] for g in listed["sA"]["groups"]) == ["ga", "gab"]
        r = await m.patch(f"{U}/{uid}", json={"display_name": "Ann"})
        assert got(r) == ["ga", "gab"]
    async with login_scoped(app, "tchA", [{"role": "teacher", "building_id": A}]) as t:
        assert got(await t.get(f"{U}/{uid}")) == ["ga", "gab"]
    # a room teacher reads school-wide (legacy): unchanged
    async with login_scoped(app, "tchRoom", [{"role": "teacher", "room_id": a1}]) as t:
        assert got(await t.get(f"{U}/{uid}")) == everything


async def test_limited_reader_sees_only_own_teachers_and_members_of_a_group(app, admin):
    A, B, a1, *_ , users = await school(admin)
    tA = await mk(admin, "tA", [{"role": "teacher", "building_id": A}])
    tB = await mk(admin, "tB", [{"role": "teacher", "building_id": B}])
    tOrg = await mk(admin, "tOrg", [{"role": "teacher"}])
    ga = (await admin.post(G, json={"name": "ga", "building_ids": [A]})).json()
    members = [users[n]["id"] for n in ("sA", "sB", "org")]  # one of theirs, two they can't see
    assert (await admin.put(f"{G}/{ga['id']}/members", json=members)).status_code == 200
    assert (await admin.put(f"{G}/{ga['id']}/teachers", json=[tA["id"], tB["id"], tOrg["id"]])).status_code == 200

    def teachers(g):
        return sorted(t["username"] for t in g["teachers"])

    one = (await admin.get(f"{G}/{ga['id']}")).json()
    assert teachers(one) == ["tA", "tB", "tOrg"] and one["member_count"] == 3
    async with login_scoped(app, "mgrA", [{"role": "manager", "building_id": A}]) as m:
        one = (await m.get(f"{G}/{ga['id']}")).json()
        assert teachers(one) == ["tA"] and one["member_count"] == 1
        (listed,) = (await m.get(G)).json()
        assert teachers(listed) == ["tA"] and listed["member_count"] == 1
        assert [t["username"] for t in (await m.get(f"{G}/{ga['id']}/teachers")).json()] == ["tA"]
        # editing returns the same restricted view and keeps what they can't see
        r = await m.put(f"{G}/{ga['id']}/teachers", json=[tA["id"]])
        assert [t["username"] for t in r.json()] == ["tA"]
        r = await m.patch(f"{G}/{ga['id']}", json={"description": "d"})
        assert teachers(r.json()) == ["tA"] and r.json()["member_count"] == 1
    assert teachers((await admin.get(f"{G}/{ga['id']}")).json()) == ["tA", "tB", "tOrg"]
    # a building teacher is limited the same way; a room teacher (org-wide reader) sees all
    async with login_scoped(app, "tchA", [{"role": "teacher", "building_id": A}]) as t:
        one = (await t.get(f"{G}/{ga['id']}")).json()
        assert teachers(one) == ["tA"] and one["member_count"] == 1
    async with login_scoped(app, "tchRoom", [{"role": "teacher", "room_id": a1}]) as t:
        one = (await t.get(f"{G}/{ga['id']}")).json()
        assert teachers(one) == ["tA", "tB", "tOrg"] and one["member_count"] == 3
