# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Every route of the API, called by someone who isn't signed in and by a student, with
the ids of real objects: all must refuse (401/403/404/405), except the few that are
public on purpose or a student's own account. A new endpoint that forgets its
permission check fails here. Routes come from the OpenAPI schema (plus the hidden ones).

A control run checks that an admin really reaches the same objects, so a refusal
isn't just a wrong id."""

import re

import pytest

from tests.conftest import login_as
from tests.test_agent import FakeAgent, approve

# Public on purpose. Agent endpoints authenticate by signature (test_agent.py).
PUBLIC = {
    ("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/agent/join"), ("POST", "/api/v1/agent/join/status"), ("POST", "/api/v1/agent/heartbeat"),
}
# A signed-in student's own account.
STUDENT_OK = {
    ("GET", "/api/v1/auth/me"), ("POST", "/api/v1/auth/password"), ("PATCH", "/api/v1/auth/preferences"),
    ("GET", "/api/v1/modules"), ("GET", "/api/v1/dashboard"),
}
# Not in the schema (include_in_schema=False) but behind a login: the API docs (admins).
HIDDEN = {("GET", "/api/openapi.json"), ("GET", "/api/docs")}
REFUSED = {401, 403, 404, 405}


@pytest.fixture(autouse=True)
def no_sinkhole(monkeypatch):
    """No DNS sinkhole runs in tests: blocklist changes would answer 502 otherwise."""

    async def synced(org_id) -> None:
        return None

    monkeypatch.setattr("smp.modules.dns.api.sync_org", synced)


def _id(obj) -> str:
    """The first "id" in a response, however it's nested."""
    if isinstance(obj, dict):
        if isinstance(obj.get("id"), str):
            return obj["id"]
        for value in obj.values():
            if found := _id(value):
                return found
    return ""


async def real_objects(app, admin) -> dict[str, str]:
    """An object of every kind a path parameter can name."""

    async def made(path: str, body: dict) -> dict:
        r = await admin.post(path, json=body)
        assert r.status_code in (200, 201), (path, r.status_code, r.text[:200])  # a broken setup must not pass quietly
        return r.json()

    building = await made("/api/v1/buildings", {"name": "Sweep building"})
    room = await made("/api/v1/rooms", {"name": "Sweep room", "subnets": ["10.77.0.0/24"], "building_id": building["id"]})
    group = await made("/api/v1/groups", {"name": "Sweep 9.A", "kind": "class"})
    user = await made("/api/v1/users", {"username": "sweep.victim", "password": "Victim-Pass-1234",
                                        "roles": [{"role": "teacher"}]})
    policy = await made("/api/v1/policies", {"kind": "classroom", "name": "Sweep", "data": {}})
    assignment = await made("/api/v1/policy-assignments", {"policy_id": policy["id"], "target_type": "room",
                                                           "target_id": room["id"]})
    schedule = await made("/api/v1/schedules", {"name": "Sweep", "timezone": "Europe/Budapest",
                                                "windows": [{"days": [0], "start": "08:00", "end": "09:00"}]})
    pc = FakeAgent(app, ip="10.77.0.5", hostname="SWEEP-PC", mac="aa:bb:cc:77:00:05")
    await pc.join()
    device_id = await approve(admin, pc, room_mode="fixed", room_id=room["id"])
    waiting = FakeAgent(app, ip="10.77.0.6", hostname="SWEEP-PC2", mac="aa:bb:cc:77:00:06")
    await waiting.join()
    (request,) = [r for r in (await admin.get("/api/v1/agent/join-requests")).json() if r["hostname"] == "SWEEP-PC2"]
    entry = await made("/api/v1/dns/blocklist", {"domain": "sweep-blocked.example", "reason": "x"})
    link = await made("/api/v1/shortlinks/links", {"target_url": "https://example.org", "code": "sweep1"})
    token = await made("/api/v1/tokens", {"name": "sweep", "permissions": ["core.devices.read"]})
    ids = {
        "building_id": building["id"], "room_id": room["id"], "group_id": group["id"], "user_id": user["id"], "policy_id": policy["id"],
        "assignment_id": _id(assignment), "schedule_id": _id(schedule), "device_id": device_id,
        "request_id": request["id"], "entry_id": _id(entry), "link_id": _id(link), "token_id": _id(token),
        "code": link.get("code", "sweep1"), "job_id": "0" * 32, "name": "core",
        "file_name": "smp-agent.exe", "domain": "sweep-blocked.example",
    }
    assert all(ids.values()), ids
    return ids


def fill(path: str, ids: dict[str, str]) -> str:
    def value(m: re.Match) -> str:
        name = m.group(1).split(":")[0]
        return ids.get(name, ids["entry_id"] if name.endswith("id") else "x")

    return re.sub(r"\{([^}]+)\}", value, path)


def routes(app) -> list[tuple[str, str]]:
    found = {(method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops}
    return sorted(found | HIDDEN)


async def test_nobody_and_students_are_refused_everywhere(app, admin, anon):
    ids = await real_objects(app, admin)
    problems = []
    async with login_as(app, admin, "sweep.student", [{"role": "student"}]) as student:
        for method, path in routes(app):
            if (method, path) in PUBLIC:
                continue
            body = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
            url = fill(path, ids)
            if (r := await anon.request(method, url, **body)).status_code not in REFUSED:
                problems.append(("not signed in", method, path, r.status_code))
            if (method, path) in STUDENT_OK:
                continue
            if (r := await student.request(method, url, **body)).status_code not in REFUSED:
                problems.append(("student", method, path, r.status_code))
    assert not problems, problems
    assert len(routes(app)) > 80  # the schema really lists the API (not just a few routes)


async def test_control_an_admin_reaches_the_same_objects(app, admin):
    ids = await real_objects(app, admin)
    for path in ("/api/v1/devices/{device_id}", "/api/v1/rooms/{room_id}", "/api/v1/buildings/{building_id}", "/api/v1/users/{user_id}",
                 "/api/v1/classroom/rooms/{room_id}", "/api/v1/modules/{name}/settings"):
        r = await admin.get(fill(path, ids))
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
