# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Software deployment: uploads, packages, assignments, what agents get and report."""

import hashlib
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from sqlalchemy import update

from smp.core import db
from smp.core.config import get_settings
from smp.modules.agent.crypto import signed_message
from smp.modules.software import storage
from smp.modules.software.desired import PENDING, Target, effective_state, pick
from smp.modules.software.models import SoftwareDeviceState, SoftwareUpload
from tests.conftest import login_as
from tests.test_agent import FakeAgent, approve, b64url

MSI = storage.MSI_MAGIC + os.urandom(4000)
EXE = b"MZ" + os.urandom(3000)


@pytest.fixture
def software_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "software_dir", str(tmp_path))
    return tmp_path


async def upload(admin, content: bytes = MSI, name: str = "7z2408-x64.msi") -> httpx.Response:
    return await admin.post("/api/v1/software/uploads", params={"filename": name}, content=content,
                            headers={"content-type": "application/octet-stream"})


async def make_package(admin, content: bytes = MSI, file_name: str = "7z.msi", **fields) -> dict:
    r = await upload(admin, content, file_name)
    assert r.status_code == 201, r.text
    r = await admin.post("/api/v1/software/packages", json={"upload_id": r.json()["id"], "name": "7-Zip", **fields})
    assert r.status_code == 201, r.text
    return r.json()


async def assign(admin, package: dict, target_type: str = "org", target_id: str | None = None,
                 action: str = "install") -> httpx.Response:
    return await admin.post("/api/v1/software/assignments", json={
        "package_id": package["id"], "target_type": target_type, "target_id": target_id, "action": action})


async def pc(app, admin, hostname: str, room: dict | None, n: int) -> tuple[FakeAgent, str]:
    agent = FakeAgent(app, hostname=hostname, mac=f"aa:bb:cc:00:10:{n:02x}")
    await agent.join()
    device_id = await approve(admin, agent, room_mode="fixed", room_id=room["id"] if room else None)
    return agent, device_id


async def software_config(agent: FakeAgent) -> dict | None:
    agent.config_version = None
    r = await agent.heartbeat()
    assert r.status_code == 200, r.text
    return r.json()["config"].get("software")


async def signed(agent: FakeAgent, method: str, path: str, raw: bytes = b"", headers: dict | None = None) -> httpx.Response:
    ts = str(int(time.time()))
    der = agent.key.sign(signed_message(method, path, ts, raw), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    h = {"x-smp-key": agent.fingerprint, "x-smp-time": ts,
         "x-smp-signature": b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big")),
         "content-type": "application/json", **(headers or {})}
    transport = httpx.ASGITransport(app=agent.app, client=(agent.ip, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, content=raw, headers=h)


async def report(agent: FakeAgent, *reports: dict) -> httpx.Response:
    import json

    return await signed(agent, "POST", "/api/v1/software/agent/status", json.dumps({"reports": list(reports)}).encode())


# --------------------------------------------------------------------------- uploads and packages


async def test_upload_and_describe_a_package(app, admin, software_dir):
    r = await upload(admin)
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["kind"] == "msi" and up["size"] == len(MSI) and up["sha256"] == hashlib.sha256(MSI).hexdigest()
    assert (software_dir / "files" / up["sha256"]).read_bytes() == MSI
    assert list((software_dir / "incoming").iterdir()) == []
    # The same content again: a new upload row, one file.
    again = (await upload(admin, name=r"C:\Users\admin\Downloads\copy.msi")).json()
    assert again["sha256"] == up["sha256"] and again["file_name"] == "copy.msi"
    assert len(list((software_dir / "files").iterdir())) == 1

    r = await admin.post("/api/v1/software/packages", json={
        "upload_id": up["id"], "name": "7-Zip", "version": "24.08", "install_args": "INSTALLDIR=\"C:\\7z\"",
        "reboot": "if_required"})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["detection"] == {"type": "msi", "product_code": None}  # the .msi default
    assert p["removable"] is True and p["kind"] == "msi" and p["reboot"] == "if_required"
    (listed,) = (await admin.get("/api/v1/software/packages")).json()
    assert listed["id"] == p["id"] and listed["counts"] == {} and listed["assignments"] == 0
    actions = {e["action"] for e in (await admin.get("/api/v1/audit", params={"action": "software."})).json()}
    assert {"software.upload.create", "software.package.create"} <= actions


async def test_only_installers_are_accepted(app, admin, software_dir, monkeypatch):
    for content, name in [(MSI, "setup.zip"), (EXE, "setup.msi"), (MSI, "setup.exe"), (b"", "empty.msi"),
                          (b"PK\x03\x04" + os.urandom(100), "fake.exe")]:
        r = await upload(admin, content, name)
        assert r.status_code == 422 and r.json()["code"] == "software.bad_file", (name, r.text)
    assert (await upload(admin, EXE, "setup.EXE")).json()["kind"] == "exe"
    monkeypatch.setattr(get_settings(), "software_max_upload_bytes", 1000)
    r = await upload(admin)
    assert r.status_code == 413 and r.json()["code"] == "software.too_large"
    assert list((software_dir / "incoming").iterdir()) == []  # nothing half-written left
    monkeypatch.setattr(get_settings(), "software_dir", None)
    assert (await upload(admin)).json()["code"] == "software.no_storage"


async def test_exe_packages_need_detection_and_an_uninstall_command_to_be_removed(app, admin, software_dir):
    up = (await upload(admin, EXE, "vlc-3.0.21-win64.exe")).json()
    base = {"upload_id": up["id"], "name": "VLC", "install_args": "/L=1038 /S"}
    r = await admin.post("/api/v1/software/packages", json=base)
    assert r.status_code == 422 and r.json()["code"] == "software.detection_required"
    r = await admin.post("/api/v1/software/packages", json={**base, "detection": {"type": "msi"}})
    assert r.json()["code"] == "software.msi_detection_needs_msi"
    for bad in [
        {"type": "registry", "key": r"HKCU\Software\VLC"},  # the agent runs as SYSTEM
        {"type": "registry", "key": r"HKLM\SOFTWARE\VLC", "equals": "3.0"},  # equals without value
        {"type": "file", "path": r"vlc\vlc.exe"},  # not absolute
        {"type": "file", "path": "C:\\VLC\\vlc.exe\n"},
    ]:
        assert (await admin.post("/api/v1/software/packages", json={**base, "detection": bad})).status_code == 422, bad
    r = await admin.post("/api/v1/software/packages", json={**base, "install_args": "/S\r\ndel C:\\x"})
    assert r.status_code == 422  # one line only

    det = {"type": "registry", "key": r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\VLC media player",
           "value": "DisplayVersion", "equals": "3.0.21"}
    p = (await admin.post("/api/v1/software/packages", json={**base, "detection": det})).json()
    assert p["removable"] is False and p["detection"] == {"type": "registry", **{k: v for k, v in det.items() if k != "type"}}
    r = await assign(admin, p, action="remove")
    assert r.status_code == 409 and r.json()["code"] == "software.not_removable"

    uninstall = r'"%ProgramFiles%\VideoLAN\VLC\uninstall.exe" /S'
    r = await admin.patch(f"/api/v1/software/packages/{p['id']}", json={"uninstall_command": uninstall})
    assert r.status_code == 200 and r.json()["removable"] is True
    assert (await assign(admin, p, action="remove")).status_code == 201
    r = await admin.patch(f"/api/v1/software/packages/{p['id']}", json={"uninstall_command": ""})
    assert r.status_code == 409 and r.json()["code"] == "software.not_removable"
    r = await admin.patch(f"/api/v1/software/packages/{p['id']}", json={"name": None})
    assert r.status_code == 422


# --------------------------------------------------------------------------- what computers get


def test_the_most_specific_assignment_wins():
    pkg, other, room, dev = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    targets = [
        Target(pkg, "org", None, None, None, "install"),
        Target(pkg, "room", None, room, None, "remove"),
        Target(pkg, "device", None, None, dev, "install"),
        Target(other, "room", None, uuid.uuid4(), None, "install"),  # another room
    ]
    assert pick(targets, dev, room) == {pkg: "install"}  # the computer's own assignment
    assert pick(targets, uuid.uuid4(), room) == {pkg: "remove"}  # its room's
    assert pick(targets, uuid.uuid4(), None) == {pkg: "install"}  # the school's
    assert pick([], dev, room) == {}


def test_a_building_sits_between_the_school_and_a_room():
    pkg, building, room, dev = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    targets = [
        Target(pkg, "org", None, None, None, "install"),
        Target(pkg, "building", building, None, None, "remove"),
    ]
    assert pick(targets, dev, room, building) == {pkg: "remove"}  # the building beats the school
    assert pick(targets, dev, room, uuid.uuid4()) == {pkg: "install"}  # another building's computer
    assert pick(targets, dev, None) == {pkg: "install"}  # in no room, so in no building
    targets.append(Target(pkg, "room", None, room, None, "install"))
    assert pick(targets, dev, room, building) == {pkg: "install"}  # the room beats the building
    targets.append(Target(pkg, "device", None, None, dev, "remove"))
    assert pick(targets, dev, room, building) == {pkg: "remove"}  # the computer beats the room


async def test_agents_get_their_packages(app, admin, software_dir):
    lab = (await admin.post("/api/v1/rooms", json={"name": "Lab 1"})).json()
    office = (await admin.post("/api/v1/rooms", json={"name": "Office"})).json()
    a1, _ = await pc(app, admin, "LAB1-PC01", lab, 1)
    a2, d2 = await pc(app, admin, "LAB1-PC02", lab, 2)
    o1, _ = await pc(app, admin, "OFFICE-PC01", office, 3)
    assert await software_config(a1) is None  # nothing assigned: no section at all

    zip7 = await make_package(admin, reboot="always")
    vlc = await make_package(admin, EXE, "vlc.exe", name="VLC", detection={"type": "file", "path": r"%ProgramFiles%\VLC\vlc.exe"},
                             uninstall_command=r'"C:\Program Files\VLC\uninstall.exe" /S')
    assert (await assign(admin, zip7)).status_code == 201  # everywhere
    assert (await assign(admin, vlc, "room", lab["id"])).status_code == 201
    assert (await assign(admin, zip7, "room", office["id"], "remove")).status_code == 201
    assert (await assign(admin, vlc, "device", d2, "remove")).status_code == 201
    r = await assign(admin, vlc, "room", lab["id"])
    assert r.status_code == 409 and r.json()["code"] == "software.assignment_exists"

    cfg = await software_config(a1)
    assert [(p["name"], p["action"]) for p in cfg["packages"]] == [("7-Zip", "install"), ("VLC", "install")]
    entry = cfg["packages"][0]
    assert entry["url"] == f"/api/v1/software/agent/packages/{zip7['id']}" and entry["sha256"] == zip7["sha256"]
    assert entry["reboot"] == "always" and entry["kind"] == "msi" and entry["detection"]["type"] == "msi"
    assert cfg["status_path"] == "/api/v1/software/agent/status" and cfg["spread_s"] == 120
    assert [(p["name"], p["action"]) for p in (await software_config(a2))["packages"]] == \
        [("7-Zip", "install"), ("VLC", "remove")]
    assert [(p["name"], p["action"]) for p in (await software_config(o1))["packages"]] == [("7-Zip", "remove")]

    # Deleting an assignment doesn't uninstall: the computer just isn't told anything.
    (office_remove,) = [a for a in (await admin.get(f"/api/v1/software/packages/{zip7['id']}/assignments")).json()
                        if a["target_type"] == "room"]
    assert office_remove["target_name"] == "Office"
    assert (await admin.delete(f"/api/v1/software/assignments/{office_remove['id']}")).status_code == 204
    assert [(p["name"], p["action"]) for p in (await software_config(o1))["packages"]] == [("7-Zip", "install")]
    r = await admin.patch(f"/api/v1/software/assignments/{office_remove['id']}", json={"action": "remove"})
    assert r.status_code == 404


async def test_assignment_changes_wake_waiting_agents(app, admin, software_dir, monkeypatch):
    import asyncio

    monkeypatch.setattr(get_settings(), "agent_longpoll_seconds", 5)
    agent, _ = await pc(app, admin, "LAB1-PC01", None, 1)
    await agent.heartbeat()
    package = await make_package(admin)
    waiting = asyncio.create_task(agent.heartbeat(wait=True))
    await asyncio.sleep(0.2)
    started = time.monotonic()
    assert (await assign(admin, package)).status_code == 201
    r = await waiting
    assert time.monotonic() - started < 3
    assert r.json()["config"]["software"]["packages"][0]["id"] == package["id"]


# --------------------------------------------------------------------------- downloads


async def test_only_assigned_computers_download_the_file(app, admin, software_dir):
    lab = (await admin.post("/api/v1/rooms", json={"name": "Lab 1"})).json()
    a1, _ = await pc(app, admin, "LAB1-PC01", lab, 1)
    other, _ = await pc(app, admin, "OFFICE-PC01", None, 2)
    package = await make_package(admin)
    path = f"/api/v1/software/agent/packages/{package['id']}"
    assert (await signed(a1, "GET", path)).status_code == 404  # not assigned yet
    await assign(admin, package, "room", lab["id"])

    r = await signed(a1, "GET", path)
    assert r.status_code == 200 and r.content == MSI and r.headers["x-checksum-sha256"] == package["sha256"]
    assert r.headers["accept-ranges"] == "bytes"
    # An interrupted download continues where it stopped.
    r = await signed(a1, "GET", path, headers={"range": "bytes=1000-"})
    assert r.status_code == 206 and r.content == MSI[1000:]
    assert r.headers["content-range"] == f"bytes 1000-{len(MSI) - 1}/{len(MSI)}"
    assert (await signed(a1, "GET", path, headers={"range": f"bytes={len(MSI)}-"})).status_code == 416

    r = await signed(other, "GET", path)
    assert r.status_code == 404 and r.json()["code"] == "software.not_assigned"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get(path)).status_code == 401  # not public: installers can hold license keys
    assert (await admin.get(path)).status_code == 401  # nor for a browser session


async def test_downloads_wait_their_turn(app, admin, software_dir):
    r = await admin.put("/api/v1/modules/software/settings", json={"max_parallel_downloads": 1})
    assert r.status_code == 200, r.text
    agent, _ = await pc(app, admin, "LAB1-PC01", None, 1)
    package = await make_package(admin)
    await assign(admin, package)
    slots = app.state.registry.get("software").slots
    busy = slots.try_acquire(1)  # another computer is downloading
    try:
        r = await signed(agent, "GET", f"/api/v1/software/agent/packages/{package['id']}")
        assert r.status_code == 503 and r.json()["code"] == "software.busy"
        assert 20 <= int(r.headers["retry-after"]) <= 60
    finally:
        busy.release()
    r = await signed(agent, "GET", f"/api/v1/software/agent/packages/{package['id']}")
    assert r.status_code == 200 and slots.active == 0  # the slot is free again afterwards


def test_download_slots_and_ranges():
    now = [0.0]
    slots = storage.DownloadSlots(stale_after=100, clock=lambda: now[0])
    a = slots.try_acquire(2)
    b = slots.try_acquire(2)
    assert a and b and slots.try_acquire(2) is None
    a.release()
    a.release()  # twice is fine
    assert slots.active == 1 and slots.try_acquire(2) is not None
    now[0] = 101  # leases that were never released expire
    assert slots.try_acquire(1) is not None

    assert storage.parse_range(None, 100) is None
    assert storage.parse_range("bytes=10-", 100) == (10, 99)
    assert storage.parse_range("bytes=10-19", 100) == (10, 19)
    assert storage.parse_range("bytes=90-500", 100) == (90, 99)
    assert storage.parse_range("bytes=-30", 100) == (70, 99)
    assert storage.parse_range("bytes=0-1,5-6", 100) is None  # several ranges: whole file
    for bad in ("bytes=100-", "bytes=20-10", "bytes=-0"):
        with pytest.raises(storage.RangeNotSatisfiable):
            storage.parse_range(bad, 100)


# --------------------------------------------------------------------------- reports and status


async def test_reports_show_up_per_package_and_computer(app, admin, software_dir):
    lab = (await admin.post("/api/v1/rooms", json={"name": "Lab 1"})).json()
    a1, d1 = await pc(app, admin, "LAB1-PC01", lab, 1)
    a2, d2 = await pc(app, admin, "LAB1-PC02", lab, 2)
    package = await make_package(admin)
    await assign(admin, package, "room", lab["id"])
    counts = lambda: admin.get(f"/api/v1/software/packages/{package['id']}")  # noqa: E731
    assert (await counts()).json()["counts"] == {"pending": 2}

    assert (await report(a1, {"package_id": package["id"], "state": "installed", "sha256": package["sha256"],
                              "exit_code": 0})).status_code == 204
    assert (await report(a2, {"package_id": package["id"], "state": "installing", "sha256": package["sha256"]},
                         {"package_id": package["id"], "state": "failed", "sha256": package["sha256"],
                          "exit_code": 1603, "message": "Fatal error\nduring installation"})).status_code == 204
    assert (await counts()).json()["counts"] == {"installed": 1, "failed": 1}
    rows = {r["hostname"]: r for r in (await admin.get(f"/api/v1/software/packages/{package['id']}/devices")).json()}
    assert rows["LAB1-PC02"]["state"] == "failed" and rows["LAB1-PC02"]["exit_code"] == 1603
    assert rows["LAB1-PC02"]["message"] == "Fatal error during installation" and rows["LAB1-PC01"]["room_name"] == "Lab 1"
    (state,) = (await admin.get(f"/api/v1/software/devices/{d1}")).json()
    assert state["name"] == "7-Zip" and state["action"] == "install" and state["state"] == "installed"
    widgets = {w["id"]: w for w in (await admin.get("/api/v1/dashboard")).json()}
    assert widgets["software.installations"]["value"] == 1

    # A new version of the file: everyone is pending again until they report on it.
    new = (await upload(admin, storage.MSI_MAGIC + os.urandom(5000), "7z2409-x64.msi")).json()
    r = await admin.patch(f"/api/v1/software/packages/{package['id']}", json={"upload_id": new["id"], "version": "24.09"})
    assert r.status_code == 200 and r.json()["sha256"] == new["sha256"] and r.json()["counts"] == {"pending": 2}
    (entry,) = (await software_config(a1))["packages"]
    assert entry["sha256"] == new["sha256"] and entry["version"] == "24.09"

    # Nonsense is refused; reports about other schools' or unknown packages are ignored.
    assert (await report(a1, {"package_id": package["id"], "state": "hacked"})).status_code == 422
    assert (await report(a1, {"package_id": str(uuid.uuid4()), "state": "installed"})).status_code == 204
    async with db.sessionmaker()() as s:
        assert len((await s.execute(SoftwareDeviceState.__table__.select())).all()) == 2
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anon:
        r = await anon.post("/api/v1/software/agent/status", json={"reports": []})
        assert r.status_code == 401


def test_effective_state():
    row = SoftwareDeviceState(state="installed", sha256="a" * 64)
    assert effective_state("install", row, "a" * 64) == "installed"
    assert effective_state("install", row, "b" * 64) == PENDING  # about an older file
    assert effective_state("install", None, "a" * 64) == PENDING
    assert effective_state("remove", row, "a" * 64) == PENDING  # not removed yet
    assert effective_state("remove", SoftwareDeviceState(state="removed", sha256=None), "a" * 64) == "removed"
    assert effective_state(None, row, "b" * 64) == "installed"  # unassigned: what it last said
    assert effective_state(None, None, "a" * 64) is None


async def test_teachers_see_their_rooms_and_change_nothing(app, admin, software_dir):
    lab = (await admin.post("/api/v1/rooms", json={"name": "Lab 1"})).json()
    other = (await admin.post("/api/v1/rooms", json={"name": "Lab 2"})).json()
    a1, d1 = await pc(app, admin, "LAB1-PC01", lab, 1)
    _, d2 = await pc(app, admin, "LAB2-PC01", other, 2)
    package = await make_package(admin)
    await assign(admin, package)
    await assign(admin, package, "room", other["id"], "remove")
    await report(a1, {"package_id": package["id"], "state": "failed", "sha256": package["sha256"], "exit_code": 1603})

    async with login_as(app, admin, "tanar", [{"role": "teacher", "room_id": lab["id"]}]) as teacher:
        (listed,) = (await teacher.get("/api/v1/software/packages")).json()
        assert listed["counts"] == {"failed": 1}  # Lab 2's computer isn't counted
        hosts = [r["hostname"] for r in (await teacher.get(f"/api/v1/software/packages/{package['id']}/devices")).json()]
        assert hosts == ["LAB1-PC01"]
        assert [a["target_type"] for a in (await teacher.get(
            f"/api/v1/software/packages/{package['id']}/assignments")).json()] == ["org"]
        assert (await teacher.get(f"/api/v1/software/devices/{d1}")).status_code == 200
        assert (await teacher.get(f"/api/v1/software/devices/{d2}")).status_code == 403
        widgets = {w["id"]: w for w in (await teacher.get("/api/v1/dashboard")).json()}
        assert widgets["software.installations"]["value"] == 1
        # Installing software runs code as SYSTEM: admins only.
        assert (await assign(teacher, package, "room", lab["id"])).status_code == 403
        assert (await upload(teacher)).status_code == 403
        assert (await teacher.patch(f"/api/v1/software/packages/{package['id']}", json={"name": "x"})).status_code == 403
    async with login_as(app, admin, "diak", [{"role": "student"}]) as student:
        assert (await student.get("/api/v1/software/packages")).status_code == 403


async def test_deleting_packages_and_cleaning_up_files(app, admin, software_dir):
    package = await make_package(admin)
    r = await assign(admin, package)
    r = await admin.delete(f"/api/v1/software/packages/{package['id']}")
    assert r.status_code == 409 and r.json()["code"] == "software.package_in_use"
    (a,) = (await admin.get(f"/api/v1/software/packages/{package['id']}/assignments")).json()
    await admin.delete(f"/api/v1/software/assignments/{a['id']}")
    assert (await admin.delete(f"/api/v1/software/packages/{package['id']}")).status_code == 204

    file = software_dir / "files" / package["sha256"]
    async with db.sessionmaker()() as s:
        assert await storage.collect_garbage(s) == 0  # a day's grace: the form may still be open
        assert file.exists()
        long_ago = datetime.now(UTC) - timedelta(days=2)
        await s.execute(update(SoftwareUpload).values(created_at=long_ago))
        await s.commit()
        os.utime(file, (time.time() - 7200, time.time() - 7200))
        assert await storage.collect_garbage(s) == 1
    assert not file.exists()
