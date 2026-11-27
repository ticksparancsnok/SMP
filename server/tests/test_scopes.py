# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Scoped roles: the role table, how a Principal is built from assignments (org / building /
room scope), audit attribution to buildings, and migration 0017 on real data."""

import asyncio
import threading
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import asyncpg
import pytest
from sqlalchemy import select

from smp.core import db, roles
from smp.core.context import Ctx, Principal, ScopedRole, principal_for_user
from smp.core.models import AuditLog, Building, Device, Organization, Policy, Role, RoleAssignment, Room, User
from tests.conftest import TEST_DB_URL

DEVICES_WRITE = "core.devices.write"  # building-level
USERS_READ = "core.users.read"  # building-level, teacher default
LOCK = "classroom.screens.lock"  # room-scoped
DNS_WRITE = "dns.blocklist.write"  # org-wide
RESET = "core.users.reset_class_password"  # org-wide


# --------------------------------------------------------------------------- the role table


def test_role_table():
    assert [r.value for r in Role] == ["admin", "unit_manager", "manager", "teacher", "student"]
    assert set(roles.ROLE_DEFS) == {r.value for r in Role}
    ranks = [roles.rank(r) for r in ("admin", "unit_manager", "manager", "teacher", "student")]
    assert ranks == sorted(ranks, reverse=True) and len(set(ranks)) == 5
    assert roles.allowed_scope("manager", "building") and not roles.allowed_scope("manager", "org")
    assert roles.allowed_scope("teacher", "room") and not roles.allowed_scope("student", "room")
    assert roles.allowed_scope("unit_manager", "org") and not roles.allowed_scope("unit_manager", "building")
    assert roles.can_grant("manager", "teacher") and not roles.can_grant("manager", "manager")
    assert roles.can_grant("unit_manager", "manager") and not roles.can_grant("unit_manager", "unit_manager")
    assert all(roles.can_grant("admin", r) for r in roles.ROLE_DEFS)
    assert not roles.can_grant("teacher", "student") and not roles.allowed_scope("nonsense", "org")
    assert roles.with_inherited("unit_manager") == ["unit_manager", "manager", "teacher"]


# --------------------------------------------------------------------------- a small school


class School:
    """Buildings A and B; rooms a1, a2 (in A), b1 (in B), loose (in none)."""

    def __init__(self, org: Organization, registry):
        self.org, self.registry = org, registry
        self.A = self.B = None
        self.a1 = self.a2 = self.b1 = self.loose = None

    async def user(self, s, name: str, *assignments: tuple[str, Room | None, Building | None]) -> Principal:
        u = User(
            org_id=self.org.id,
            username=name,
            roles=[
                RoleAssignment(
                    org_id=self.org.id, role=role, room_id=room.id if room else None, building_id=b.id if b else None
                )
                for role, room, b in assignments
            ],
        )
        s.add(u)
        await s.commit()
        return await self.principal(s, u.id)

    async def principal(self, s, user_id) -> Principal:
        s.expire_all()
        user = await s.get(User, user_id)
        return await principal_for_user(s, user, self.registry)


@pytest.fixture
async def school(app, org):
    school = School(org, app.state.registry)
    async with db.sessionmaker()() as s:
        school.A = Building(org_id=org.id, name="A")
        school.B = Building(org_id=org.id, name="B")
        s.add_all([school.A, school.B])
        await s.flush()
        school.a1 = Room(org_id=org.id, name="a1", building_id=school.A.id)
        school.a2 = Room(org_id=org.id, name="a2", building_id=school.A.id)
        school.b1 = Room(org_id=org.id, name="b1", building_id=school.B.id)
        school.loose = Room(org_id=org.id, name="loose")
        s.add_all([school.a1, school.a2, school.b1, school.loose])
        await s.commit()
    return school


# --------------------------------------------------------------------------- the principal


async def test_building_manager(school):
    A, B = school.A, school.B
    async with db.sessionmaker()() as s:
        p = await school.user(s, "mgr", ("manager", None, A))
    # building-level powers: only for rooms / the building they manage
    assert p.can(DEVICES_WRITE, room_id=school.a1.id) and p.can(DEVICES_WRITE, room_id=school.a2.id)
    assert not p.can(DEVICES_WRITE, room_id=school.b1.id)
    assert not p.can(DEVICES_WRITE, room_id=school.loose.id)
    assert not p.can(DEVICES_WRITE)  # org-wide: fail-closed
    assert p.can(DEVICES_WRITE, building_id=A.id) and not p.can(DEVICES_WRITE, building_id=B.id)
    assert p.can_anywhere(DEVICES_WRITE)
    assert p.rooms_with(DEVICES_WRITE) == {school.a1.id, school.a2.id}
    assert p.buildings_with(DEVICES_WRITE) == {A.id}
    assert p.scope_building_ids == {A.id}
    assert p.room_buildings == {school.a1.id: A.id, school.a2.id: A.id}
    assert DEVICES_WRITE not in p.global_perms and DEVICES_WRITE in p.building_perms[A.id]
    # inherited teacher permissions, also only inside the building
    assert p.can(USERS_READ, building_id=A.id) and not p.can(USERS_READ)
    # room-scoped permissions are expanded to the building's rooms
    assert p.can(LOCK, room_id=school.a1.id) and p.can(LOCK, room_id=school.a2.id)
    assert not p.can(LOCK, room_id=school.b1.id) and not p.can(LOCK)
    assert p.rooms_with(LOCK) == {school.a1.id, school.a2.id}
    assert p.buildings_with(LOCK) == set()
    assert p.room_perms[school.a1.id] >= {LOCK, "core.devices.read"}
    # school-wide data is org-wide even from a building role
    assert p.can(DNS_WRITE) and p.can(RESET) and p.rooms_with(DNS_WRITE) is None and p.buildings_with(DNS_WRITE) is None
    # never the settings or building admin
    assert not p.can_anywhere("core.settings.write") and not p.can_anywhere("core.buildings.write")
    assert not p.can_anywhere("core.tokens.manage") and not p.can_anywhere("software.packages.write")
    assert p.can_anywhere("software.assignments.write") and not p.can("software.assignments.write")
    assert p.roles == [ScopedRole("manager", None, A.id)]


async def test_manager_of_two_buildings_and_new_rooms(school):
    A, B = school.A, school.B
    async with db.sessionmaker()() as s:
        p = await school.user(s, "mgr2", ("manager", None, A), ("manager", None, B))
        assert p.buildings_with(DEVICES_WRITE) == {A.id, B.id}
        assert p.rooms_with(DEVICES_WRITE) == {school.a1.id, school.a2.id, school.b1.id}
        assert not p.can(DEVICES_WRITE, room_id=school.loose.id)
        # a room moved into a building is covered at once (the principal is built per request)
        room = await s.get(Room, school.loose.id)
        room.building_id = B.id
        await s.commit()
        p = await school.principal(s, p.id)
        assert p.can(DEVICES_WRITE, room_id=school.loose.id) and p.can(LOCK, room_id=school.loose.id)


async def test_unit_manager_and_admin(school):
    async with db.sessionmaker()() as s:
        um = await school.user(s, "um", ("unit_manager", None, None))
        admin = await school.user(s, "adm", ("admin", None, None))
    for perm in (DEVICES_WRITE, USERS_READ, LOCK, DNS_WRITE, "core.buildings.write", "core.tokens.manage",
                 "shortlinks.links.manage_all", "software.packages.write", "software.assignments.write", "core.audit.read"):
        assert um.can(perm), perm
        assert um.can(perm, room_id=school.b1.id), perm
    for perm in ("core.settings.write", "core.settings.read"):
        assert not um.can_anywhere(perm), perm
    assert um.rooms_with(DEVICES_WRITE) is None and um.buildings_with(DEVICES_WRITE) is None
    assert "*" in admin.global_perms and admin.can("core.settings.write")
    assert admin.all_permissions(school.registry) == sorted(school.registry.permissions)


async def test_teachers_and_students(school):
    A = school.A
    async with db.sessionmaker()() as s:
        org_teacher = await school.user(s, "t_org", ("teacher", None, None))
        room_teacher = await school.user(s, "t_room", ("teacher", school.a1, None))
        bld_teacher = await school.user(s, "t_bld", ("teacher", None, A))
        student = await school.user(s, "s_org", ("student", None, None))
        bld_student = await school.user(s, "s_bld", ("student", None, A))
    assert org_teacher.can(USERS_READ)
    assert org_teacher.can(LOCK) and org_teacher.can(LOCK, room_id=school.b1.id)  # org-wide: every room
    assert org_teacher.rooms_with(LOCK) is None
    # a room teacher: unchanged legacy behaviour (room-scoped perms for the room, the rest org-wide)
    assert room_teacher.can(LOCK, room_id=school.a1.id) and not room_teacher.can(LOCK, room_id=school.a2.id)
    assert not room_teacher.can(LOCK)
    assert room_teacher.can(USERS_READ) and room_teacher.can(DNS_WRITE)
    assert room_teacher.rooms_with(LOCK) == {school.a1.id}
    assert room_teacher.building_perms == {} and room_teacher.room_buildings == {}
    # a building teacher: room-scoped perms for the building's rooms, school-wide data, no org-wide reads
    assert bld_teacher.can(LOCK, room_id=school.a2.id) and not bld_teacher.can(LOCK, room_id=school.b1.id)
    assert not bld_teacher.can(USERS_READ) and bld_teacher.can(USERS_READ, building_id=A.id)
    assert bld_teacher.can(DNS_WRITE) and bld_teacher.can(RESET)
    assert not bld_teacher.can_anywhere(DEVICES_WRITE)
    # students have no permissions at all, wherever they are
    assert student.global_perms == frozenset() and bld_student.global_perms == frozenset()
    assert bld_student.building_perms == {} and bld_student.rooms_with(LOCK) == set()
    assert bld_student.scope_building_ids == {A.id}


# --------------------------------------------------------------------------- audit attribution


def _ctx(school, principal: Principal, session) -> Ctx:
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(registry=school.registry, bus=None)), client=None, headers={}
    )
    return Ctx(request, session, principal, school.org.id)  # type: ignore[arg-type]


async def test_audit_building_ids(school):
    A, B = school.A, school.B
    async with db.sessionmaker()() as s:
        p = await school.user(s, "aud", ("admin", None, None))
        ctx = _ctx(school, p, s)
        device = Device(org_id=school.org.id, hostname="pc1", room_id=school.a1.id)
        loose_device = Device(org_id=school.org.id, hostname="pc2", room_id=school.loose.id)
        no_room = Device(org_id=school.org.id, hostname="pc3")
        policy = Policy(org_id=school.org.id, kind="x", name="p", building_id=B.id)
        s.add_all([device, loose_device, no_room, policy])
        await s.flush()
        user = await s.get(User, p.id)
        ctx.audit("t.room", school.b1)  # a Room: its building_id
        ctx.audit("t.building", A)  # a Building: itself
        ctx.audit("t.policy", policy)  # has building_id
        ctx.audit("t.device", device)  # room_id: looked up by the INSERT
        ctx.audit("t.loose_device", loose_device)  # room without a building: NULL
        ctx.audit("t.no_room", no_room)  # no room: NULL
        ctx.audit("t.explicit", device, building_ids=[A.id, B.id])  # explicit wins
        ctx.audit("t.org_level", user)  # org-level target: NULL
        ctx.audit("t.nothing")  # no target: NULL
        ctx.audit("t.loose_room", school.loose)  # a room in no building
        await s.commit()
        rows = (await s.execute(select(AuditLog).where(AuditLog.action.like("t.%")))).scalars().all()
    got = {r.action: r.building_ids for r in rows}
    assert got == {
        "t.room": [B.id],
        "t.building": [A.id],
        "t.policy": [B.id],
        "t.device": [A.id],
        "t.loose_device": None,
        "t.no_room": None,
        "t.explicit": [A.id, B.id],
        "t.org_level": None,
        "t.nothing": None,
        "t.loose_room": None,
    }


async def test_role_assignment_constraints(school):
    from sqlalchemy.exc import IntegrityError

    async with db.sessionmaker()() as s:
        u = User(org_id=school.org.id, username="c")
        s.add(u)
        await s.flush()
        s.add(RoleAssignment(org_id=school.org.id, user_id=u.id, role="teacher", room_id=school.a1.id, building_id=school.A.id))
        with pytest.raises(IntegrityError):  # one scope only
            await s.flush()
        await s.rollback()
    async with db.sessionmaker()() as s:
        u = User(org_id=school.org.id, username="c2")
        s.add(u)
        await s.flush()
        for _ in range(2):  # the same building twice: NULLs are equal, so it's a duplicate
            s.add(RoleAssignment(org_id=school.org.id, user_id=u.id, role="manager", building_id=school.A.id))
        with pytest.raises(IntegrityError):
            await s.flush()


# --------------------------------------------------------------------------- migration 0017

MIG_DB = urlsplit(TEST_DB_URL.replace("+asyncpg", "")).path.lstrip("/") + "_mig"


def _alembic(action: str, revision: str) -> None:
    """Run an Alembic command on the migration database, in a thread (Alembic's env.py
    starts its own event loop)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", "smp:migrations")
    cfg.set_main_option("sqlalchemy.url", urlsplit(TEST_DB_URL)._replace(path="/" + MIG_DB).geturl().replace("%", "%%"))
    err: list[BaseException] = []

    def target():
        try:
            getattr(command, action)(cfg, revision)
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=target)
    t.start()
    t.join()
    if err:
        raise err[0]


def _dsn(db_name: str) -> str:
    return urlsplit(TEST_DB_URL.replace("+asyncpg", ""))._replace(path="/" + db_name).geturl()


def _sql(fn):
    """Run `fn(connection)` against the migration database."""

    async def go():
        conn = await asyncpg.connect(_dsn(MIG_DB))
        try:
            return await fn(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


def test_migration_0017_buildings_roundtrip():
    async def recreate():
        conn = await asyncpg.connect(_dsn("postgres"))
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{MIG_DB}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{MIG_DB}"')
        finally:
            await conn.close()

    asyncio.run(recreate())
    _alembic("upgrade", "0016_agent_capabilities")

    org1, org2 = uuid.uuid4(), uuid.uuid4()
    rooms = {name: uuid.uuid4() for name in ("r_main1", "r_main2", "r_none", "r_annex", "r_other_org")}
    user = uuid.uuid4()

    async def seed(c):
        for org, slug in ((org1, "one"), (org2, "two")):
            await c.execute("INSERT INTO organizations (id, slug, name, timezone) VALUES ($1, $2, $2, 'UTC')", org, slug)
        for name, building, org in (
            ("r_main1", " Main ", org1),
            ("r_main2", "Main", org1),
            ("r_none", "", org1),
            ("r_annex", "Annex", org1),
            ("r_other_org", "Main", org2),
        ):
            await c.execute(
                "INSERT INTO rooms (id, org_id, name, building, description, subnets) VALUES ($1, $2, $3, $4, '', '[]')",
                rooms[name], org, name, building,
            )
        await c.execute(
            "INSERT INTO users (id, org_id, username, display_name, auth_source, disabled) VALUES ($1, $2, 'u', '', 'local', false)",
            user, org1,
        )
        await c.execute(
            "INSERT INTO role_assignments (id, org_id, user_id, role, room_id) VALUES ($1, $2, $3, 'teacher', $4)",
            uuid.uuid4(), org1, user, rooms["r_annex"],
        )

    _sql(seed)
    _alembic("upgrade", "head")

    async def check_up(c):
        cols = {r["column_name"] for r in await c.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'rooms'")}
        assert "building" not in cols and "building_id" in cols
        buildings = await c.fetch("SELECT id, org_id, name, description, created_at FROM buildings")
        assert sorted((str(b["org_id"]), b["name"]) for b in buildings) == sorted(
            [(str(org1), "Main"), (str(org1), "Annex"), (str(org2), "Main")]
        )
        by = {(b["org_id"], b["name"]): b["id"] for b in buildings}
        linked = {r["name"]: r["building_id"] for r in await c.fetch("SELECT name, building_id FROM rooms")}
        assert linked == {
            "r_main1": by[(org1, "Main")],
            "r_main2": by[(org1, "Main")],
            "r_none": None,
            "r_annex": by[(org1, "Annex")],
            "r_other_org": by[(org2, "Main")],
        }
        # the existing room-scoped role assignment survived; a building-scoped one can be added
        assert await c.fetchval("SELECT count(*) FROM role_assignments WHERE room_id IS NOT NULL") == 1
        await c.execute(
            "INSERT INTO role_assignments (id, org_id, user_id, role, building_id) VALUES ($1, $2, $3, 'manager', $4)",
            uuid.uuid4(), org1, user, by[(org1, "Main")],
        )
        # building-owned policies may share a name with a school-wide one
        for owner in (None, by[(org1, "Main")]):
            await c.execute(
                "INSERT INTO policies (id, org_id, kind, name, description, data, building_id) VALUES ($1, $2, 'dns', 'same', '', '{}', $3)",
                uuid.uuid4(), org1, owner,
            )
        return by

    by = _sql(check_up)

    _alembic("downgrade", "0016_agent_capabilities")

    async def check_down(c):
        text_of = {r["name"]: r["building"] for r in await c.fetch("SELECT name, building FROM rooms")}
        assert text_of == {"r_main1": "Main", "r_main2": "Main", "r_none": "", "r_annex": "Annex", "r_other_org": "Main"}
        assert await c.fetchval("SELECT count(*) FROM role_assignments") == 1  # the building-scoped one is gone
        assert not await c.fetchval("SELECT to_regclass('buildings')")
        names = sorted(r["name"] for r in await c.fetch("SELECT name FROM policies"))
        assert names == ["same", "same (Main)"]
        return by

    _sql(check_down)
    _alembic("upgrade", "head")  # and forward again on the downgraded data


# --------------------------------------------------------------------------- API surface


async def test_rooms_api_building(app, admin):
    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization))).scalar_one()
        bld = Building(org_id=org.id, name="Main")
        other = Building(org_id=org.id, name="Annex")
        s.add_all([bld, other])
        await s.commit()
        bid, oid = str(bld.id), str(other.id)
    r = await admin.post("/api/v1/rooms", json={"name": "R1", "building_id": bid})
    assert r.status_code == 201, r.text
    assert r.json()["building_id"] == bid and r.json()["building"] == "Main"
    room_id = r.json()["id"]
    r = await admin.patch(f"/api/v1/rooms/{room_id}", json={"building_id": oid})
    assert r.status_code == 200 and r.json()["building"] == "Annex"
    r = await admin.patch(f"/api/v1/rooms/{room_id}", json={"building_id": None})
    assert r.json()["building_id"] is None and r.json()["building"] == ""
    assert (await admin.post("/api/v1/rooms", json={"name": "R2", "building_id": str(uuid.uuid4())})).status_code == 404
    # the audit entries of the moves name both buildings; the creation names the building
    async with db.sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.action.like("core.room.%")).order_by(AuditLog.id))).scalars().all()
    assert [r.building_ids for r in rows] == [[uuid.UUID(bid)], [uuid.UUID(bid), uuid.UUID(oid)], [uuid.UUID(oid)]]


async def test_auth_me_keeps_fields_and_adds_building_scope(app, admin):
    me = (await admin.get("/api/v1/auth/me")).json()
    for key in ("id", "username", "display_name", "org_id", "org_name", "roles", "permissions", "room_permissions",
                "must_change_password", "preferences"):
        assert key in me, key
    assert me["roles"] == [{"role": "admin", "room_id": None, "building_id": None}]
    assert me["building_permissions"] == {} and me["room_buildings"] == {}
    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization))).scalar_one()
        bld = Building(org_id=org.id, name="Main")
        s.add(bld)
        await s.flush()
        room = Room(org_id=org.id, name="m1", building_id=bld.id)
        from smp.core.security import hash_password

        s.add_all([room, User(org_id=org.id, username="mgr", password_hash=hash_password("pw-pw-pw-12"),
                              roles=[RoleAssignment(org_id=org.id, role="manager", building_id=bld.id)])])
        await s.commit()
        bid, rid = str(bld.id), str(room.id)
    from tests.conftest import make_client

    async with make_client(app) as c:
        r = await c.post("/api/v1/auth/login", json={"username": "mgr", "password": "pw-pw-pw-12"})
        assert r.status_code == 200, r.text
        me = r.json()
        assert me["roles"] == [{"role": "manager", "room_id": None, "building_id": bid}]
        assert me["room_buildings"] == {rid: bid}
        assert "core.devices.write" in me["building_permissions"][bid]
        assert "core.devices.write" not in me["permissions"]  # never org-wide
        assert "dns.blocklist.write" in me["permissions"]
        assert "core.settings.write" not in me["permissions"]
        assert (await c.get("/api/v1/auth/me")).json() == me
        assert (await c.get("/openapi.json")).status_code in (401, 403, 404)  # docs guard: admin only
