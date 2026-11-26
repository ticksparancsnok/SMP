# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Buildings in the software and agent modules: the building assignment target, managers
assigning only inside their buildings, the building hint of join requests and who may
approve them, and the install command."""

import uuid

import pytest
from sqlalchemy import select

from smp.core import db
from smp.core.config import get_settings
from smp.core.models import AuditLog
from smp.modules.agent import AgentModule
from tests.conftest import login_scoped, make_building
from tests.test_agent import FakeAgent
from tests.test_software import assign, make_package, upload

JOIN = "/api/v1/agent/join"
REQUESTS = "/api/v1/agent/join-requests"


@pytest.fixture
def software_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "software_dir", str(tmp_path))
    return tmp_path


_macs = iter(range(1, 10_000))


def new_agent(app, hostname: str) -> FakeAgent:
    n = next(_macs)
    return FakeAgent(app, hostname=hostname, mac=f"aa:bb:cc:01:{n >> 8:02x}:{n & 255:02x}")


async def join(agent: FakeAgent, building=...) -> dict:
    """What the agent sends, plus (unless `...`) the building hint."""
    body = {
        "public_key": agent.public_key, "hostname": agent.hostname, "mac": agent.mac,
        "os_name": "Windows 11 Pro", "os_version": "10.0.26100", "agent_version": "0.1.0",
    }
    if building is not ...:
        body["building"] = building
    r = await agent.post(JOIN, body)
    assert r.status_code == 200, r.text
    return r.json()


async def request_of(client, agent: FakeAgent) -> dict | None:
    r = await client.get(REQUESTS)
    assert r.status_code == 200, r.text
    return next((x for x in r.json() if x["fingerprint"] == agent.fingerprint), None)


async def approve_as(client, agent: FakeAgent, **body):
    req = await request_of(client, agent)
    assert req is not None
    return await client.post(f"{REQUESTS}/{req['id']}/approve", json=body)


async def pc_in(app, admin, hostname: str, room_id: str | None) -> tuple[FakeAgent, str]:
    """An approved computer in a room (by the admin)."""
    agent = new_agent(app, hostname)
    await join(agent)
    r = await approve_as(admin, agent, room_mode="fixed", room_id=room_id)
    assert r.status_code == 200, r.text
    return agent, r.json()["device_id"]


async def package_action(agent: FakeAgent, package: dict) -> str | None:
    agent.config_version = None
    r = await agent.heartbeat()
    assert r.status_code == 200, r.text
    entries = (r.json()["config"].get("software") or {}).get("packages", [])
    return next((e["action"] for e in entries if e["id"] == package["id"]), None)


# --------------------------------------------------------------------------- software: the building target


async def test_computers_get_the_most_specific_target_org_building_room_device(app, admin, software_dir):
    b1, (r1,) = await make_building("Main", ["Lab"])
    _, (r_other,) = await make_building("Annex", ["Annex lab"])
    inside, d_inside = await pc_in(app, admin, "MAIN-PC01", r1)
    outside, _ = await pc_in(app, admin, "ANNEX-PC01", r_other)
    roomless, _ = await pc_in(app, admin, "FREE-PC01", None)
    package = await make_package(admin)

    assert (await assign(admin, package, "org")).status_code == 201
    assert [await package_action(a, package) for a in (inside, outside, roomless)] == ["install"] * 3
    r = await assign(admin, package, "building", b1, "remove")
    assert r.status_code == 201, r.text
    assert r.json()["target_type"] == "building" and r.json()["target_id"] == b1 and r.json()["target_name"] == "Main"
    # The building beats the school, for its computers only.
    assert [await package_action(a, package) for a in (inside, outside, roomless)] == ["remove", "install", "install"]
    assert (await assign(admin, package, "room", r1, "install")).status_code == 201  # the room beats the building
    assert await package_action(inside, package) == "install"
    assert (await assign(admin, package, "device", d_inside, "remove")).status_code == 201  # the computer beats both
    assert await package_action(inside, package) == "remove"
    assert await package_action(outside, package) == "install"

    # The same building twice is refused; an unknown building is a 404.
    assert (await assign(admin, package, "building", b1)).status_code == 409
    assert (await assign(admin, package, "building", str(uuid.uuid4()))).status_code == 404
    # The agent's payload keeps its shape.
    inside.config_version = None
    config = (await inside.heartbeat()).json()["config"]
    assert set(config["software"]) == {"packages", "status_path", "spread_s", "reboot_warning_s"}
    assert set(config["software"]["packages"][0]) == {
        "id", "name", "version", "action", "kind", "file_name", "size", "sha256", "url", "install_args",
        "uninstall_command", "detection", "reboot", "success_codes", "timeout_s"}


async def test_a_room_moving_between_buildings_changes_what_its_computers_get(app, admin, software_dir):
    b1, (r1,) = await make_building("North", ["N1"])
    b2, _ = await make_building("South", ["S1"])
    agent, _ = await pc_in(app, admin, "N-PC01", r1)
    package = await make_package(admin)
    await assign(admin, package, "building", b2)
    assert await package_action(agent, package) is None
    assert (await admin.patch(f"/api/v1/rooms/{r1}", json={"building_id": b2})).status_code == 200
    assert await package_action(agent, package) == "install"


# --------------------------------------------------------------------------- software: who may assign what


async def test_managers_assign_only_inside_their_buildings(app, admin, software_dir):
    b1, (r1a, r1b) = await make_building("Main", ["A1", "A2"])
    b2, (r2,) = await make_building("Annex", ["B1"])
    _, d1 = await pc_in(app, admin, "MAIN-PC01", r1a)
    _, d2 = await pc_in(app, admin, "ANNEX-PC01", r2)
    _, d0 = await pc_in(app, admin, "FREE-PC01", None)
    package = await make_package(admin)
    foreign = (await assign(admin, package, "building", b2)).json()
    school = (await assign(admin, package, "org")).json()

    async with login_scoped(app, "manager1", [{"role": "manager", "building_id": b1}]) as manager:
        for target_type, target_id in (("building", b1), ("room", r1a), ("device", d1)):
            r = await assign(manager, package, target_type, target_id)
            assert r.status_code == 201, (target_type, r.text)
        # Not the whole school, not another building's rooms or computers, not a computer in no room.
        for target_type, target_id in (("org", None), ("building", b2), ("room", r2), ("device", d2), ("device", d0)):
            r = await assign(manager, package, target_type, target_id)
            assert r.status_code == 403, (target_type, r.text)
            assert r.json()["code"] == "permission.missing"
        # Changing and deleting: their own yes, the school's and other buildings' no.
        mine = [a for a in (await manager.get(f"/api/v1/software/packages/{package['id']}/assignments")).json()
                if a["target_type"] == "building"]
        assert [a["target_id"] for a in mine] == [b1]
        for other in (foreign, school):
            assert (await manager.delete(f"/api/v1/software/assignments/{other['id']}")).status_code == 403
            assert (await manager.patch(f"/api/v1/software/assignments/{other['id']}", json={"action": "install"})).status_code == 403
        assert (await manager.delete(f"/api/v1/software/assignments/{mine[0]['id']}")).status_code == 204
        # Rooms of the building they were not assigned to are not theirs either.
        assert (await assign(manager, package, "room", r1b)).status_code == 201


async def test_managers_switch_their_assignments_between_install_and_remove(app, admin, software_dir):
    b1, (r1,) = await make_building("Main", ["A1"])
    b2, _ = await make_building("Annex")
    package = await make_package(admin)  # .msi: removable
    own = (await assign(admin, package, "building", b1)).json()
    foreign = (await assign(admin, package, "building", b2)).json()
    async with login_scoped(app, "manager1", [{"role": "manager", "building_id": b1}]) as manager:
        r = await manager.patch(f"/api/v1/software/assignments/{own['id']}", json={"action": "remove"})
        assert r.status_code == 200 and r.json()["action"] == "remove" and r.json()["target_name"] == "Main"
        assert (await manager.patch(f"/api/v1/software/assignments/{foreign['id']}", json={"action": "remove"})).status_code == 403


async def test_managers_cannot_upload_or_change_packages_unit_managers_can(app, admin, software_dir):
    b1, _ = await make_building("Main", ["A1"])
    package = await make_package(admin)
    async with login_scoped(app, "manager1", [{"role": "manager", "building_id": b1}]) as manager:
        assert (await upload(manager)).status_code == 403
        assert (await manager.post("/api/v1/software/packages", json={"upload_id": str(uuid.uuid4()), "name": "x"})).status_code == 403
        assert (await manager.patch(f"/api/v1/software/packages/{package['id']}", json={"name": "x"})).status_code == 403
        assert (await manager.delete(f"/api/v1/software/packages/{package['id']}")).status_code == 403
        assert (await manager.get("/api/v1/software/packages")).status_code == 200  # they can read them
    async with login_scoped(app, "unit1", [{"role": "unit_manager"}]) as unit:
        mine = await make_package(unit, content=b"MZ" + b"\x01" * 3000, file_name="tool.exe",
                                  detection={"type": "file", "path": "C:\\tool.exe"})
        assert (await unit.patch(f"/api/v1/software/packages/{mine['id']}", json={"name": "Tool 2"})).status_code == 200
        school = await assign(unit, mine, "org")
        assert school.status_code == 201  # school-wide too
        assert (await assign(unit, mine, "building", b1)).status_code == 201
        assert (await unit.delete(f"/api/v1/software/packages/{mine['id']}")).status_code == 409  # still assigned
        assert (await unit.delete(f"/api/v1/software/assignments/{school.json()['id']}")).status_code == 204


async def test_assignment_lists_hide_other_buildings(app, admin, software_dir):
    b1, (r1,) = await make_building("Main", ["A1"])
    b2, (r2,) = await make_building("Annex", ["B1"])
    _, d1 = await pc_in(app, admin, "MAIN-PC01", r1)
    _, d2 = await pc_in(app, admin, "ANNEX-PC01", r2)
    package = await make_package(admin)
    for target_type, target_id in (("org", None), ("building", b1), ("building", b2), ("room", r1), ("room", r2),
                                   ("device", d1), ("device", d2)):
        assert (await assign(admin, package, target_type, target_id)).status_code == 201
    url = f"/api/v1/software/packages/{package['id']}/assignments"

    def seen(rows):
        return sorted((a["target_type"], a["target_id"]) for a in rows)

    assert len((await admin.get(url)).json()) == 7
    async with login_scoped(app, "manager1", [{"role": "manager", "building_id": b1}]) as manager:
        assert seen((await manager.get(url)).json()) == sorted(
            [("org", None), ("building", b1), ("room", r1), ("device", d1)])
    async with login_scoped(app, "teacher2", [{"role": "teacher", "room_id": r2}]) as teacher:
        assert seen((await teacher.get(url)).json()) == sorted(
            [("org", None), ("building", b2), ("room", r2), ("device", d2)])
        assert (await assign(teacher, package, "room", r2, "remove")).status_code == 403
    async with login_scoped(app, "teacher1", [{"role": "teacher", "building_id": b1}]) as teacher:
        assert len((await teacher.get(url)).json()) == 4
        assert (await assign(teacher, package, "building", b1)).status_code == 403
    async with login_scoped(app, "unit1", [{"role": "unit_manager"}]) as unit:
        assert len((await unit.get(url)).json()) == 7


async def test_managers_see_the_status_of_their_buildings_computers_only(app, admin, software_dir):
    b1, (r1,) = await make_building("Main", ["A1"])
    _, (r2,) = await make_building("Annex", ["B1"])
    await pc_in(app, admin, "MAIN-PC01", r1)
    await pc_in(app, admin, "ANNEX-PC01", r2)
    package = await make_package(admin)
    await assign(admin, package, "org")
    async with login_scoped(app, "manager1", [{"role": "manager", "building_id": b1}]) as manager:
        hosts = [d["hostname"] for d in (await manager.get(f"/api/v1/software/packages/{package['id']}/devices")).json()]
        assert hosts == ["MAIN-PC01"]


# --------------------------------------------------------------------------- agent: the building hint


async def test_the_join_hint_resolves_by_name_or_id(app, admin):
    b1, _ = await make_building("Main Building", ["A1"])
    cases = [("Main Building", b1), ("  main BUILDING ", b1), (b1, b1), (b1.upper(), b1), ("Nowhere", None),
             ("", None), ("   ", None)]
    for hint, expected in cases:
        agent = new_agent(app, "PC-HINT")
        assert (await join(agent, hint))["status"] == "pending"
        req = await request_of(admin, agent)
        assert req["building_id"] == expected, hint
        assert req["building_hint"] == (hint.strip() or None)
        assert req["building_name"] == ("Main Building" if expected else None)
    # No hint at all (the body of an agent from before buildings).
    agent = new_agent(app, "PC-OLD")
    assert set(await join(agent)) == {"status", "fingerprint", "fingerprint_display", "server_time"}
    req = await request_of(admin, agent)
    assert req["building_hint"] is None and req["building_id"] is None and req["building_name"] is None
    assert (await join(agent, None))["status"] == "pending"  # an explicit null too


async def test_a_repeated_join_updates_the_hint(app, admin):
    b1, _ = await make_building("Main", ["A1"])
    b2, _ = await make_building("Annex")
    agent = new_agent(app, "PC-LATE")
    await join(agent)  # the first join comes without a hint
    assert (await request_of(admin, agent))["building_id"] is None
    await join(agent, "main")
    req = await request_of(admin, agent)
    assert (req["building_hint"], req["building_id"]) == ("main", b1)
    await join(agent, "Annex")
    req = await request_of(admin, agent)
    assert (req["building_hint"], req["building_id"]) == ("Annex", b2)
    await join(agent, "typo")  # the hint is replaced even if it matches nothing now
    req = await request_of(admin, agent)
    assert (req["building_hint"], req["building_id"]) == ("typo", None)
    await join(agent)  # an agent that says nothing doesn't erase what it said
    assert (await request_of(admin, agent))["building_hint"] == "typo"
    assert sum(1 for r in (await admin.get(REQUESTS)).json() if r["fingerprint"] == agent.fingerprint) == 1


async def test_the_hint_is_limited_to_200_characters(app, admin):
    agent = new_agent(app, "PC-LONG")
    r = await agent.post(JOIN, {"public_key": agent.public_key, "hostname": "PC-LONG", "building": "x" * 201})
    assert r.status_code == 422


# --------------------------------------------------------------------------- agent: who sees and approves what


async def test_managers_see_and_approve_only_their_buildings_requests(app, admin):
    b1, (r1a, r1b) = await make_building("Main", ["A1", "A2"])
    b2, (r2,) = await make_building("Annex", ["B1"])
    mine, theirs, nobody = new_agent(app, "MAIN-NEW"), new_agent(app, "ANNEX-NEW"), new_agent(app, "UNKNOWN-NEW")
    await join(mine, "Main")
    await join(theirs, "Annex")
    await join(nobody)

    async with login_scoped(app, "manager1", [{"role": "manager", "building_id": b1}]) as manager:
        seen = (await manager.get(REQUESTS)).json()
        assert [r["fingerprint"] for r in seen] == [mine.fingerprint] and seen[0]["building_name"] == "Main"
        req = seen[0]
        base = f"{REQUESTS}/{req['id']}"
        # Requests of other buildings and without one are not theirs, whatever they try.
        for other in (theirs, nobody):
            other_req = await request_of(admin, other)
            assert (await manager.post(f"{REQUESTS}/{other_req['id']}/approve",
                                       json={"room_mode": "fixed", "room_id": r1a})).status_code == 403
            assert (await manager.post(f"{REQUESTS}/{other_req['id']}/reject")).status_code == 403
        # Their own: a room of their building is required, and it must be theirs.
        r = await manager.post(f"{base}/approve", json={"room_mode": "fixed"})
        assert r.status_code == 422 and r.json()["code"] == "agent.room_required"
        r = await manager.post(f"{base}/approve", json={"room_mode": "fixed", "room_id": r2})
        assert r.status_code == 403
        r = await manager.post(f"{base}/approve", json={"room_mode": "fixed", "room_id": str(uuid.uuid4())})
        assert r.status_code == 404
        r = await manager.post(f"{base}/approve", json={"room_mode": "network"})  # no subnet maps to a room of theirs
        assert r.status_code == 403 and r.json()["code"] == "agent.room_out_of_scope"
        assert await request_of(admin, mine) is not None  # nothing happened yet
        r = await manager.post(f"{base}/approve", json={"room_mode": "fixed", "room_id": r1b})
        assert r.status_code == 200, r.text
        device_id = r.json()["device_id"]
    assert await mine.status() == "approved"
    assert (await mine.heartbeat()).status_code == 200
    assert (await admin.get(f"/api/v1/devices/{device_id}")).json()["room_id"] == r1b
    # The audit entry belongs to the building, for its managers.
    async with db.sessionmaker()() as s:
        entry = (await s.execute(select(AuditLog).where(AuditLog.action == "agent.join.approve"))).scalars().one()
        assert entry.building_ids == [uuid.UUID(b1)]
    # The others are still waiting, for the school-wide ones.
    assert {r["hostname"] for r in (await admin.get(REQUESTS)).json()} == {"ANNEX-NEW", "UNKNOWN-NEW"}


async def test_unit_managers_handle_every_request_without_choosing_a_room(app, admin):
    await make_building("Main", ["A1"])
    one, two = new_agent(app, "PC-ONE"), new_agent(app, "PC-TWO")
    await join(one, "Main")
    await join(two)
    async with login_scoped(app, "unit1", [{"role": "unit_manager"}]) as unit:
        assert {r["hostname"] for r in (await unit.get(REQUESTS)).json()} == {"PC-ONE", "PC-TWO"}
        assert (await approve_as(unit, two, room_mode="fixed")).status_code == 200  # no room needed
        (req,) = (await unit.get(REQUESTS)).json()
        assert (await unit.post(f"{REQUESTS}/{req['id']}/reject")).status_code == 204


async def test_managers_take_over_and_reset_only_their_buildings_computers(app, admin):
    b1, (r1,) = await make_building("Main", ["A1"])
    b2, (r2,) = await make_building("Annex", ["B1"])
    _, d_own = await pc_in(app, admin, "MAIN-PC01", r1)
    _, d_foreign = await pc_in(app, admin, "ANNEX-PC01", r2)
    _, d_free = await pc_in(app, admin, "FREE-PC01", None)
    async with login_scoped(app, "manager1", [{"role": "manager", "building_id": b1}]) as manager:
        # Reinstalled PCs ask again: taking over a computer is for whoever has it in scope.
        for device_id, ok in ((d_foreign, False), (d_free, False), (d_own, True)):
            agent = new_agent(app, "MAIN-PC01")
            await join(agent, "Main")
            r = await approve_as(manager, agent, device_id=device_id, room_mode="fixed", room_id=r1)
            assert r.status_code == (200 if ok else 403), (device_id, r.text)
            if ok:
                assert r.json()["device_id"] == device_id
        # Reset identity: the same rule, by the computer's room.
        assert (await manager.post(f"/api/v1/agent/devices/{d_foreign}/reset-identity")).status_code == 403
        assert (await manager.post(f"/api/v1/agent/devices/{d_free}/reset-identity")).status_code == 403
        assert (await manager.post(f"/api/v1/agent/devices/{d_own}/reset-identity")).status_code == 204
    assert (await admin.post(f"/api/v1/agent/devices/{d_foreign}/reset-identity")).status_code == 204


async def test_managers_reject_and_allow_again_their_own_requests(app, admin):
    b1, _ = await make_building("Main", ["A1"])
    agent, stranger = new_agent(app, "MAIN-NEW"), new_agent(app, "OTHER-NEW")
    await join(agent, "Main")
    await join(stranger)
    async with login_scoped(app, "manager1", [{"role": "manager", "building_id": b1}]) as manager:
        (req,) = (await manager.get(REQUESTS)).json()
        assert (await manager.post(f"{REQUESTS}/{req['id']}/reject")).status_code == 204
        assert await agent.status() == "rejected"
        assert (await manager.get(REQUESTS)).json() == []
        (rejected,) = (await manager.get(f"{REQUESTS}/rejected")).json()
        assert rejected["fingerprint"] == agent.fingerprint and rejected["building_name"] == "Main"
        # Someone else's rejection is not theirs to undo.
        other = await request_of(admin, stranger)
        assert (await admin.post(f"{REQUESTS}/{other['id']}/reject")).status_code == 204
        assert (await manager.get(f"{REQUESTS}/rejected")).json() == [rejected]
        assert (await manager.post(f"{REQUESTS}/{other['id']}/allow")).status_code == 403
        assert (await manager.post(f"{REQUESTS}/{rejected['id']}/allow")).status_code == 204
    assert await agent.status() == "unknown"


async def test_teachers_and_students_have_nothing_to_do_with_join_requests(app, admin):
    b1, (r1,) = await make_building("Main", ["A1"])
    async with login_scoped(app, "teacher1", [{"role": "teacher", "building_id": b1}]) as teacher:
        assert (await teacher.get(REQUESTS)).status_code == 403
        assert (await teacher.get("/api/v1/agent/install-command")).status_code == 403
        assert (await teacher.get("/api/v1/agent/download")).status_code == 403
    async with login_scoped(app, "student1", [{"role": "student"}]) as student:
        assert (await student.get(REQUESTS)).status_code == 403


# --------------------------------------------------------------------------- agent: install command


async def test_the_install_command_for_a_manager_has_their_building_preset(app, admin):
    b1, _ = await make_building("Main", ["A1"])
    b2, _ = await make_building("Annex")
    async with login_scoped(app, "manager1", [{"role": "manager", "building_id": b1}]) as manager:
        r = await manager.get("/api/v1/agent/install-command")
        assert r.status_code == 200, r.text
        assert r.json() == {"command": f"smp-agent.exe install --server http://test --building {b1}",
                            "building_id": b1, "building_name": "Main", "server": "http://test"}
        assert (await manager.get("/api/v1/agent/install-command", params={"building_id": b1})).json()["building_id"] == b1
        assert (await manager.get("/api/v1/agent/install-command", params={"building_id": b2})).status_code == 403
        assert (await manager.get("/api/v1/agent/download")).status_code == 200  # the Devices page needs it too
    async with login_scoped(app, "manager2", [{"role": "manager", "building_id": b1},
                                              {"role": "manager", "building_id": b2}]) as manager:
        r = await manager.get("/api/v1/agent/install-command")
        assert r.status_code == 422 and r.json()["code"] == "agent.building_required"
        r = await manager.get("/api/v1/agent/install-command", params={"building_id": b2})
        assert r.status_code == 200 and r.json()["building_name"] == "Annex" and r.json()["command"].endswith(f"--building {b2}")


async def test_the_install_command_for_admins_has_an_optional_building(app, admin):
    b1, _ = await make_building("Main", ["A1"])
    r = await admin.get("/api/v1/agent/install-command")
    assert r.status_code == 200
    assert r.json() == {"command": "smp-agent.exe install --server http://test", "building_id": None,
                        "building_name": None, "server": "http://test"}
    r = await admin.get("/api/v1/agent/install-command", params={"building_id": b1})
    assert r.json()["command"] == f"smp-agent.exe install --server http://test --building {b1}"
    assert (await admin.get("/api/v1/agent/install-command", params={"building_id": str(uuid.uuid4())})).status_code == 404
    async with login_scoped(app, "unit1", [{"role": "unit_manager"}]) as unit:
        assert (await unit.get("/api/v1/agent/install-command")).json()["building_id"] is None
        assert (await unit.get("/api/v1/agent/install-command", params={"building_id": b1})).status_code == 200


async def test_the_install_command_uses_the_host_the_request_came_to(app, admin):
    r = await admin.get("/api/v1/agent/install-command", headers={"host": "smp.school.example"})
    assert r.json()["server"] == "http://smp.school.example"


def test_building_changes_wake_waiting_heartbeats():
    assert "core.building.*" in AgentModule.WAKE_ON
