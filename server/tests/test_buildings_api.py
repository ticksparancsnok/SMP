# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Buildings API and the building-scoped write checks of rooms and devices: admins and
unit managers do everything, a building manager only inside their building, a building
teacher only reads."""

import uuid

from sqlalchemy import select

from smp.core import db
from smp.core.models import AuditLog
from tests.conftest import login_as, login_scoped, make_building

B = "/api/v1/buildings"


async def audit_buildings(action: str, target_id: str) -> list[list[str] | None]:
    async with db.sessionmaker()() as s:
        rows = await s.execute(
            select(AuditLog.building_ids).where(AuditLog.action == action, AuditLog.target_id == target_id)
        )
        return [[str(i) for i in ids] if ids else None for (ids,) in rows]


# --------------------------------------------------------------------------- CRUD


async def test_building_crud_admin(app, admin):
    r = await admin.post(B, json={"name": "Crud main", "description": "old wing"})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["name"] == "Crud main" and b["description"] == "old wing" and b["room_count"] == 0
    r = await admin.post("/api/v1/rooms", json={"name": "Crud room", "building_id": b["id"]})
    assert r.status_code == 201 and r.json()["building"] == "Crud main"
    assert (await admin.get(f"{B}/{b['id']}")).json()["room_count"] == 1
    assert b["id"] in [x["id"] for x in (await admin.get(B)).json()]

    r = await admin.patch(f"{B}/{b['id']}", json={"name": "Crud main 2"})
    assert r.status_code == 200 and r.json()["name"] == "Crud main 2" and r.json()["description"] == "old wing"
    assert (await admin.patch(f"{B}/{b['id']}", json={"description": "new"})).json()["description"] == "new"
    assert (await admin.patch(f"{B}/{b['id']}", json={"name": ""})).status_code == 422

    assert (await audit_buildings("core.building.create", b["id"])) == [[b["id"]]]
    assert len(await audit_buildings("core.building.update", b["id"])) == 2

    # not empty while it has a room; empty after the room is gone
    r = await admin.delete(f"{B}/{b['id']}")
    assert r.status_code == 409 and r.json()["code"] == "buildings.not_empty"
    room_id = (await admin.get("/api/v1/rooms")).json()
    room_id = next(x["id"] for x in room_id if x["name"] == "Crud room")
    assert (await admin.delete(f"/api/v1/rooms/{room_id}")).status_code == 204
    assert (await admin.delete(f"{B}/{b['id']}")).status_code == 204
    assert (await admin.get(f"{B}/{b['id']}")).status_code == 404
    assert (await audit_buildings("core.building.delete", b["id"])) == [[b["id"]]]


async def test_building_name_unique(app, admin):
    assert (await admin.post(B, json={"name": "Dup one"})).status_code == 201
    other = (await admin.post(B, json={"name": "Dup two"})).json()
    r = await admin.post(B, json={"name": "Dup one"})
    assert r.status_code == 409 and r.json()["code"] == "buildings.name_taken"
    r = await admin.patch(f"{B}/{other['id']}", json={"name": "Dup one"})
    assert r.status_code == 409 and r.json()["code"] == "buildings.name_taken"
    # renaming to its own name is fine
    assert (await admin.patch(f"{B}/{other['id']}", json={"name": "Dup two"})).status_code == 200


async def test_building_delete_refused_with_policy_or_schedule(app, admin):
    from smp.core.models import Organization, Policy, Schedule

    bid, _ = await make_building("Owner building")
    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization))).scalar_one()
        s.add(Schedule(org_id=org.id, building_id=uuid.UUID(bid), name="Owned schedule", windows=[]))
        await s.commit()
    r = await admin.delete(f"{B}/{bid}")
    assert r.status_code == 409 and r.json()["code"] == "buildings.not_empty"
    async with db.sessionmaker()() as s:
        for sch in (await s.execute(select(Schedule).where(Schedule.building_id == uuid.UUID(bid)))).scalars():
            await s.delete(sch)
        org = (await s.execute(select(Organization))).scalar_one()
        s.add(Policy(org_id=org.id, building_id=uuid.UUID(bid), kind="classroom", name="Owned policy", data={}))
        await s.commit()
    assert (await admin.delete(f"{B}/{bid}")).status_code == 409
    async with db.sessionmaker()() as s:
        for p in (await s.execute(select(Policy).where(Policy.building_id == uuid.UUID(bid)))).scalars():
            await s.delete(p)
        await s.commit()
    assert (await admin.delete(f"{B}/{bid}")).status_code == 204


async def test_delete_building_removes_its_policy_assignments(app, admin):
    bid, _ = await make_building("Assigned building")
    r = await admin.post("/api/v1/policies", json={"kind": "classroom", "name": "For building", "data": {}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    from smp.core.models import Organization, PolicyAssignment, TargetType

    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization))).scalar_one()
        s.add(PolicyAssignment(org_id=org.id, policy_id=uuid.UUID(pid), target_type=TargetType.BUILDING,
                               target_id=uuid.UUID(bid)))
        await s.commit()
    assert (await admin.delete(f"{B}/{bid}")).status_code == 204
    async with db.sessionmaker()() as s:
        left = (await s.execute(select(PolicyAssignment).where(PolicyAssignment.target_id == uuid.UUID(bid)))).all()
    assert left == []


async def test_unit_manager_manages_buildings(app, admin):
    async with login_scoped(app, "um.buildings", [{"role": "unit_manager"}]) as um:
        r = await um.post(B, json={"name": "UM building"})
        assert r.status_code == 201, r.text
        bid = r.json()["id"]
        assert (await um.patch(f"{B}/{bid}", json={"description": "x"})).status_code == 200
        assert bid in [x["id"] for x in (await um.get(B)).json()]
        assert (await um.get(f"{B}/{bid}")).status_code == 200
        assert (await um.delete(f"{B}/{bid}")).status_code == 204


# --------------------------------------------------------------------------- scoped readers


async def test_manager_sees_only_own_building(app, admin):
    a, _ = await make_building("Mgr A", ["Mgr A1"])
    b, _ = await make_building("Mgr B", ["Mgr B1", "Mgr B2"])
    async with login_scoped(app, "mgr.list", [{"role": "manager", "building_id": a}]) as m:
        listed = (await m.get(B)).json()
        assert [x["id"] for x in listed] == [a]
        assert listed[0]["room_count"] == 1
        assert (await m.get(f"{B}/{a}")).status_code == 200
        r = await m.get(f"{B}/{b}")
        assert r.status_code == 404  # existence not revealed
        assert (await m.get(f"{B}/{uuid.uuid4()}")).status_code == 404
        # writes need the org-wide grant
        assert (await m.post(B, json={"name": "Mgr new"})).status_code == 403
        assert (await m.patch(f"{B}/{a}", json={"name": "Renamed"})).status_code == 403
        assert (await m.delete(f"{B}/{a}")).status_code == 403
        assert (await m.delete(f"{B}/{b}")).status_code == 403


async def test_building_teacher_reads_but_does_not_write(app, admin):
    a, rooms = await make_building("Teach A", ["Teach A1"])
    b, _ = await make_building("Teach B")
    async with login_scoped(app, "teach.bld", [{"role": "teacher", "building_id": a}]) as t:
        assert [x["id"] for x in (await t.get(B)).json()] == [a]
        assert (await t.get(f"{B}/{a}")).status_code == 200
        assert (await t.get(f"{B}/{b}")).status_code == 404
        assert (await t.post(B, json={"name": "Teach new"})).status_code == 403
        assert (await t.patch(f"{B}/{a}", json={"name": "x"})).status_code == 403
        assert (await t.delete(f"{B}/{a}")).status_code == 403
        # and no room or device writes
        assert (await t.post("/api/v1/rooms", json={"name": "Teach room", "building_id": a})).status_code == 403
        assert (await t.patch(f"/api/v1/rooms/{rooms[0]}", json={"name": "x"})).status_code == 403
        assert (await t.delete(f"/api/v1/rooms/{rooms[0]}")).status_code == 403
        assert (await t.post("/api/v1/devices", json={"hostname": "T-PC", "room_id": rooms[0]})).status_code == 403


async def test_students_cannot_read_buildings(app, admin):
    async with login_as(app, admin, "student.bld", [{"role": "student"}]) as s:
        assert (await s.get(B)).status_code == 403


# --------------------------------------------------------------------------- rooms


async def test_manager_room_writes_stay_in_own_building(app, admin):
    a, (a1,) = await make_building("Rooms A", ["Rooms A1"])
    b, (b1,) = await make_building("Rooms B", ["Rooms B1"])
    loose = (await admin.post("/api/v1/rooms", json={"name": "Rooms loose"})).json()["id"]
    async with login_scoped(app, "mgr.rooms", [{"role": "manager", "building_id": a}]) as m:
        # visible rooms: exactly the building's, never the other building's or a building-less one
        assert [r["id"] for r in (await m.get("/api/v1/rooms")).json()] == [a1]
        assert (await m.get(f"/api/v1/rooms/{b1}")).status_code == 403
        assert (await m.get(f"/api/v1/rooms/{loose}")).status_code == 403

        # create: only in the own building
        r = await m.post("/api/v1/rooms", json={"name": "Rooms A2", "building_id": a})
        assert r.status_code == 201, r.text
        a2 = r.json()["id"]
        assert r.json()["building_id"] == a and r.json()["building"] == "Rooms A"
        assert await audit_buildings("core.room.create", a2) == [[a]]
        assert (await m.post("/api/v1/rooms", json={"name": "Rooms nope", "building_id": b})).status_code == 403
        assert (await m.post("/api/v1/rooms", json={"name": "Rooms nobuilding"})).status_code == 403
        assert (await m.post("/api/v1/rooms", json={"name": "Rooms ghost", "building_id": str(uuid.uuid4())})).status_code == 403
        assert a2 in [x["id"] for x in (await m.get("/api/v1/rooms")).json()]

        # edit inside the building; none outside
        r = await m.patch(f"/api/v1/rooms/{a2}", json={"description": "mine"})
        assert r.status_code == 200 and r.json()["description"] == "mine"
        assert (await m.patch(f"/api/v1/rooms/{b1}", json={"description": "x"})).status_code == 403
        assert (await m.patch(f"/api/v1/rooms/{loose}", json={"description": "x"})).status_code == 403

        # moves: not into another building, not out of buildings, not a foreign room in
        assert (await m.patch(f"/api/v1/rooms/{a2}", json={"building_id": b})).status_code == 403
        assert (await m.patch(f"/api/v1/rooms/{a2}", json={"building_id": None})).status_code == 403
        assert (await m.patch(f"/api/v1/rooms/{b1}", json={"building_id": a})).status_code == 403
        assert (await m.patch(f"/api/v1/rooms/{loose}", json={"building_id": a})).status_code == 403
        assert (await admin.get(f"/api/v1/rooms/{a2}")).json()["building_id"] == a
        # "moving" into the building it is in is not a move
        assert (await m.patch(f"/api/v1/rooms/{a2}", json={"building_id": a})).status_code == 200

        # delete
        assert (await m.delete(f"/api/v1/rooms/{b1}")).status_code == 403
        assert (await m.delete(f"/api/v1/rooms/{loose}")).status_code == 403
        assert (await m.delete(f"/api/v1/rooms/{a2}")).status_code == 204
        assert (await m.delete(f"/api/v1/rooms/{a1}")).status_code == 204


async def test_manager_of_two_buildings_moves_between_them(app, admin):
    a, (a1,) = await make_building("Two A", ["Two A1"])
    b, _ = await make_building("Two B")
    c, _ = await make_building("Two C")
    async with login_scoped(
        app, "mgr.two", [{"role": "manager", "building_id": a}, {"role": "manager", "building_id": b}]
    ) as m:
        r = await m.patch(f"/api/v1/rooms/{a1}", json={"building_id": b})
        assert r.status_code == 200 and r.json()["building_id"] == b
        assert (await m.patch(f"/api/v1/rooms/{a1}", json={"building_id": c})).status_code == 403
        assert (await m.patch(f"/api/v1/rooms/{a1}", json={"building_id": a})).status_code == 200
    # the move was audited against both buildings
    entries = await audit_buildings("core.room.update", a1)
    assert len(entries) == 2 and all(sorted(e) == sorted([a, b]) for e in entries)


async def test_room_move_audit_and_policy_event(app, admin):
    a, (a1,) = await make_building("Move A", ["Move A1"])
    b, _ = await make_building("Move B")
    events: list = []

    async def seen(ev):
        events.append(ev)

    app.state.bus.subscribe("core.policy.changed", seen)
    r = await admin.patch(f"/api/v1/rooms/{a1}", json={"building_id": b})
    assert r.status_code == 200, r.text
    (entry,) = await audit_buildings("core.room.update", a1)
    assert sorted(entry) == sorted([a, b])
    assert any(e.payload.get("room_id") == a1 for e in events)
    # out of every building
    assert (await admin.patch(f"/api/v1/rooms/{a1}", json={"building_id": None})).json()["building_id"] is None


async def test_unit_manager_handles_buildingless_rooms_and_devices(app, admin):
    a, _ = await make_building("UM rooms A")
    async with login_scoped(app, "um.rooms", [{"role": "unit_manager"}]) as um:
        r = await um.post("/api/v1/rooms", json={"name": "UM loose"})
        assert r.status_code == 201, r.text
        loose = r.json()["id"]
        r = await um.post("/api/v1/rooms", json={"name": "UM in A", "building_id": a})
        assert r.status_code == 201
        inside = r.json()["id"]
        # moves anywhere, including out of every building
        assert (await um.patch(f"/api/v1/rooms/{loose}", json={"building_id": a})).status_code == 200
        assert (await um.patch(f"/api/v1/rooms/{inside}", json={"building_id": None})).status_code == 200
        assert {loose, inside} <= {x["id"] for x in (await um.get("/api/v1/rooms")).json()}

        r = await um.post("/api/v1/devices", json={"hostname": "UM-LOOSE"})
        assert r.status_code == 201, r.text
        dev = r.json()["id"]
        assert (await um.patch(f"/api/v1/devices/{dev}", json={"room_id": loose})).status_code == 200
        assert (await um.patch(f"/api/v1/devices/{dev}", json={"room_id": None})).status_code == 200
        assert dev in [x["id"] for x in (await um.get("/api/v1/devices")).json()["items"]]
        assert (await um.delete(f"/api/v1/devices/{dev}")).status_code == 204
        assert (await um.delete(f"/api/v1/rooms/{loose}")).status_code == 204
        assert (await um.delete(f"/api/v1/rooms/{inside}")).status_code == 204


# --------------------------------------------------------------------------- devices


async def test_manager_device_writes_only_in_own_rooms(app, admin):
    a, (a1, a2) = await make_building("Dev A", ["Dev A1", "Dev A2"])
    _, (b1,) = await make_building("Dev B", ["Dev B1"])
    free = (await admin.post("/api/v1/devices", json={"hostname": "DEV-FREE"})).json()["id"]
    foreign = (await admin.post("/api/v1/devices", json={"hostname": "DEV-FOREIGN", "room_id": b1})).json()["id"]
    async with login_scoped(app, "mgr.devices", [{"role": "manager", "building_id": a}]) as m:
        # create: needs a room in the own building
        r = await m.post("/api/v1/devices", json={"hostname": "DEV-MINE", "room_id": a1})
        assert r.status_code == 201, r.text
        mine = r.json()["id"]
        assert await audit_buildings("core.device.create", mine) == [[a]]
        assert (await m.post("/api/v1/devices", json={"hostname": "DEV-X", "room_id": b1})).status_code == 403
        assert (await m.post("/api/v1/devices", json={"hostname": "DEV-X"})).status_code == 403
        assert (await m.post("/api/v1/devices", json={"hostname": "DEV-X", "room_id": str(uuid.uuid4())})).status_code == 403

        # reads: own rooms only, unassigned and foreign devices are invisible
        ids = [d["id"] for d in (await m.get("/api/v1/devices")).json()["items"]]
        assert ids == [mine]
        assert (await m.get("/api/v1/devices", params={"unassigned": True})).json()["items"] == []
        assert (await m.get(f"/api/v1/devices/{free}")).status_code == 403
        assert (await m.get(f"/api/v1/devices/{foreign}")).status_code == 403

        # edit and move inside the building
        assert (await m.patch(f"/api/v1/devices/{mine}", json={"notes": "n"})).status_code == 200
        assert (await m.patch(f"/api/v1/devices/{mine}", json={"room_id": a2})).json()["room_id"] == a2
        # not to a foreign room, not out of every room
        assert (await m.patch(f"/api/v1/devices/{mine}", json={"room_id": b1})).status_code == 403
        assert (await m.patch(f"/api/v1/devices/{mine}", json={"room_id": None})).status_code == 403
        # not someone else's devices, not even to pull them in
        assert (await m.patch(f"/api/v1/devices/{foreign}", json={"notes": "x"})).status_code == 403
        assert (await m.patch(f"/api/v1/devices/{foreign}", json={"room_id": a1})).status_code == 403
        assert (await m.patch(f"/api/v1/devices/{free}", json={"room_id": a1})).status_code == 403
        assert (await m.patch(f"/api/v1/devices/{free}", json={"notes": "x"})).status_code == 403

        # delete
        assert (await m.delete(f"/api/v1/devices/{foreign}")).status_code == 403
        assert (await m.delete(f"/api/v1/devices/{free}")).status_code == 403
        assert (await m.delete(f"/api/v1/devices/{mine}")).status_code == 204
    assert (await admin.get(f"/api/v1/devices/{free}")).json()["room_id"] is None


async def test_only_unit_level_sets_room_subnets(app, admin):
    """Subnets decide which room a network-following device lands in, school-wide: a
    manager setting a broad one could pull other buildings' machines into their room."""
    bid, (rid,) = await make_building("SubnetHall", ["sn1"])
    async with login_scoped(app, "subnet_mgr", [{"role": "manager", "building_id": bid}]) as mgr:
        r = await mgr.post("/api/v1/rooms", json={"name": "sn2", "building_id": bid, "subnets": ["10.0.0.0/8"]})
        assert r.status_code == 403 and r.json()["code"] == "rooms.subnets_unit_only", r.text
        r = await mgr.patch(f"/api/v1/rooms/{rid}", json={"subnets": ["10.0.0.0/8"]})
        assert r.status_code == 403, r.text
        # Without subnets (or leaving them as they are) managers still manage their rooms.
        assert (await mgr.post("/api/v1/rooms", json={"name": "sn3", "building_id": bid})).status_code == 201
        r = await mgr.patch(f"/api/v1/rooms/{rid}", json={"description": "x", "subnets": []})
        assert r.status_code == 200, r.text
    async with login_scoped(app, "subnet_um", [{"role": "unit_manager"}]) as um:
        r = await um.patch(f"/api/v1/rooms/{rid}", json={"subnets": ["10.9.0.0/24"]})
        assert r.status_code == 200, r.text
