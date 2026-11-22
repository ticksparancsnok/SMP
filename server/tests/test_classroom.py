"""Classroom monitoring: who may watch what, when PCs send frames, layouts, audit."""

import asyncio
import time
import uuid
from datetime import timedelta

import httpx

from smp.core.config import get_settings
from smp.modules.classroom.watch import FrameStore, WatchRegistry
from tests.conftest import login_as
from tests.test_agent import FakeAgent, approve

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200  # starts like a JPEG; content doesn't matter here


async def put_frame(agent: FakeAgent, kind: str = "thumb", body: bytes = JPEG, size: str = "480x270",
                    unchanged: bool = False) -> httpx.Response:
    path = "/api/v1/classroom/frames"
    headers = agent.headers(path, body)
    headers = {**headers, "content-type": "image/jpeg", "x-smp-frame-kind": kind, "x-smp-frame-size": size,
               "x-smp-screen-size": "1920x1080"}
    if unchanged:
        headers["x-smp-frame-unchanged"] = "1"
    # agent.headers() signs POST; frames are PUT
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    from smp.modules.agent.crypto import signed_message
    from tests.test_agent import b64url

    der = agent.key.sign(signed_message("PUT", path, headers["x-smp-time"], body), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    headers["x-smp-signature"] = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    transport = httpx.ASGITransport(app=agent.app, client=(agent.ip, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.put(path, content=body, headers=headers)


async def setup_room(app, admin, name="Lab A", n=2):
    room = (await admin.post("/api/v1/rooms", json={"name": name})).json()
    agents = []
    for i in range(n):
        a = FakeAgent(app, hostname=f"{name.replace(' ', '')}-PC{i + 1}", mac=f"aa:bb:cc:{len(name):02x}:00:{i:02x}")
        await a.join()
        await approve(admin, a, room_mode="fixed", room_id=room["id"])
        await a.heartbeat(user=f"diak{i + 1}")
        agents.append(a)
    return room, agents


def session() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- the core flow


async def test_frames_flow_only_while_watched(app, admin):
    room, (pc1, pc2) = await setup_room(app, admin)
    screen = (await pc1.heartbeat()).json()["config"]["classroom"]
    assert screen == {"active": False}  # nobody watches: no frames, no traffic
    assert (await put_frame(pc1)).status_code == 409

    async with login_as(app, admin, "teacher.a", [{"role": "teacher", "room_id": room["id"]}]) as t:
        view = (await t.get(f"/api/v1/classroom/rooms/{room['id']}")).json()
        assert [d["hostname"] for d in view["devices"]] == ["LabA-PC1", "LabA-PC2"]
        assert view["devices"][0]["user"] == "diak01" and view["devices"][0]["online"] is True
        sid = session()
        w = (await t.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": sid})).json()
        assert w["enabled"] and w["interval_s"] == 5 and all(d["thumb"] is None for d in w["devices"])

        screen = (await pc1.heartbeat()).json()["config"]["classroom"]
        assert screen["active"] and screen["width"] == 480 and screen["quality"] == 60 and screen["detail"] is False
        assert (await put_frame(pc1)).status_code == 204
        assert (await put_frame(pc1, kind="detail")).status_code == 409  # nobody enlarged it

        w = (await t.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": sid})).json()
        thumb = next(d for d in w["devices"] if d["id"] == view["devices"][0]["id"])["thumb"]
        assert thumb["width"] == 480 and thumb["screen_width"] == 1920 and 0 <= thumb["age_s"] < 5
        r = await t.get(f"/api/v1/classroom/devices/{view['devices'][0]['id']}/frame")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == JPEG
        again = await t.get(f"/api/v1/classroom/devices/{view['devices'][0]['id']}/frame",
                            headers={"if-none-match": r.headers["etag"]})
        assert again.status_code == 304 and again.content == b""  # unchanged: nothing re-sent

        # enlarge PC2: only it gets the full-size request
        await t.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": sid, "detail_device_id": view["devices"][1]["id"]})
        assert (await pc2.heartbeat()).json()["config"]["classroom"]["detail"] is True
        assert (await pc1.heartbeat()).json()["config"]["classroom"]["detail"] is False
        assert (await put_frame(pc2, kind="detail", size="1600x900")).status_code == 204

        assert (await t.delete(f"/api/v1/classroom/rooms/{room['id']}/watch", params={"session": sid})).status_code == 204
        assert (await pc1.heartbeat()).json()["config"]["classroom"] == {"active": False}
        # frames are gone with the last viewer (memory only, nothing kept)
        assert (await t.get(f"/api/v1/classroom/devices/{view['devices'][0]['id']}/frame")).status_code == 404

    actions = [e["action"] for e in (await admin.get("/api/v1/audit", params={"action": "classroom."})).json()]
    assert {"classroom.watch.start", "classroom.watch.detail", "classroom.watch.stop"} <= set(actions)


async def test_watching_wakes_waiting_agents(app, admin, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_longpoll_seconds", 5)
    room, (pc1,) = await setup_room(app, admin, n=1)

    async def start_watching_soon():
        await asyncio.sleep(0.3)
        async with login_as(app, admin, "teacher.w", [{"role": "teacher"}]) as t:
            await t.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": session()})

    t0 = time.monotonic()
    r, _ = await asyncio.gather(pc1.heartbeat(wait=True), start_watching_soon())
    assert r.json()["config"]["classroom"]["active"] is True and time.monotonic() - t0 < 3


# --------------------------------------------------------------------------- permissions


async def test_teachers_see_only_their_rooms(app, admin):
    room_a, (a1,) = await setup_room(app, admin, "Lab A", 1)
    room_b, (b1,) = await setup_room(app, admin, "Lab B", 1)
    async with login_as(app, admin, "teacher.ab", [{"role": "teacher", "room_id": room_a["id"]}]) as t:
        assert [r["name"] for r in (await t.get("/api/v1/classroom/rooms")).json()] == ["Lab A"]
        assert (await t.get(f"/api/v1/classroom/rooms/{room_b['id']}")).status_code == 403
        assert (await t.post(f"/api/v1/classroom/rooms/{room_b['id']}/watch", json={"session": session()})).status_code == 403
        b_id = (await admin.get(f"/api/v1/classroom/rooms/{room_b['id']}")).json()["devices"][0]["id"]
        assert (await t.get(f"/api/v1/classroom/devices/{b_id}/frame")).status_code == 403
    async with login_as(app, admin, "student.x", [{"role": "student"}]) as s:
        assert (await s.get("/api/v1/classroom/rooms")).status_code == 403
    assert (await put_frame(FakeAgent(app))).status_code == 401  # unknown machines can't upload


async def test_frames_must_be_jpegs(app, admin):
    room, (pc1,) = await setup_room(app, admin, n=1)
    await admin.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": session()})
    assert (await put_frame(pc1, body=b"<html>not an image</html>")).status_code == 422
    assert (await put_frame(pc1, body=b"\xff\xd8" + b"\0" * (3 * 1024 * 1024))).status_code == 422  # too big


async def test_unchanged_screen_costs_no_picture(app, admin, monkeypatch):
    room, (pc1,) = await setup_room(app, admin, n=1)
    sid = session()
    await admin.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": sid})
    # nothing to confirm yet (first frame, or the server restarted): the PC must send one
    assert (await put_frame(pc1, body=b"", unchanged=True)).json()["code"] == "classroom.need_frame"
    assert (await put_frame(pc1)).status_code == 204
    first = (await admin.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": sid})).json()["devices"][0]["thumb"]

    import smp.modules.classroom.watch as watch_mod

    later = time.time() + 30
    monkeypatch.setattr(watch_mod.time, "time", lambda: later)
    assert (await put_frame(pc1, body=b"", unchanged=True)).status_code == 204
    again = (await admin.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": sid})).json()["devices"][0]["thumb"]
    assert again["at"] == first["at"]  # same picture: same URL, the browser doesn't download it again
    assert again["age_s"] < 5  # but it's current, not stale


# --------------------------------------------------------------------------- policies


async def test_policy_controls_monitoring(app, admin):
    room, (pc1,) = await setup_room(app, admin, n=1)
    pol = (await admin.post("/api/v1/policies", json={"kind": "classroom", "name": "Weak Wi-Fi",
                                                      "data": {"interval_s": 20, "thumb_width": 320}})).json()
    await admin.post("/api/v1/policy-assignments", json={"policy_id": pol["id"], "target_type": "room", "target_id": room["id"]})
    w = (await admin.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": session()})).json()
    assert w["interval_s"] == 20
    screen = (await pc1.heartbeat()).json()["config"]["classroom"]
    assert screen["interval_s"] == 20 and screen["width"] == 320 and screen["quality"] == 60  # rest: school settings

    off = (await admin.post("/api/v1/policies", json={"kind": "classroom", "name": "Off", "data": {"enabled": False}})).json()
    await admin.post("/api/v1/policy-assignments", json={"policy_id": off["id"], "target_type": "room", "target_id": room["id"],
                                                         "priority": 10})
    w = (await admin.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": session()})).json()
    assert w["enabled"] is False
    assert (await pc1.heartbeat()).json()["config"]["classroom"] == {"active": False}


# --------------------------------------------------------------------------- layout


async def test_seating_layout(app, admin):
    room, (pc1, pc2) = await setup_room(app, admin)
    devices = (await admin.get(f"/api/v1/classroom/rooms/{room['id']}")).json()["devices"]
    body = {"mode": "grid", "rows": 3, "cols": 4, "positions": {devices[0]["id"]: [0, 3], devices[1]["id"]: [2, 0]}}
    async with login_as(app, admin, "teacher.l", [{"role": "teacher", "room_id": room["id"]}]) as t:
        assert (await t.put(f"/api/v1/classroom/rooms/{room['id']}/layout", json=body)).status_code == 200
        view = (await t.get(f"/api/v1/classroom/rooms/{room['id']}")).json()
        assert view["layout"]["mode"] == "grid" and view["layout"]["positions"][devices[0]["id"]] == [0, 3]
        assert view["can_edit_layout"] is True
        clash = {**body, "positions": {devices[0]["id"]: [1, 1], devices[1]["id"]: [1, 1]}}
        assert (await t.put(f"/api/v1/classroom/rooms/{room['id']}/layout", json=clash)).json()["code"] == "classroom.bad_layout"
        outside = {**body, "positions": {devices[0]["id"]: [5, 0]}}
        assert (await t.put(f"/api/v1/classroom/rooms/{room['id']}/layout", json=outside)).status_code == 422
        stranger = {**body, "positions": {str(uuid.uuid4()): [0, 0]}}
        assert (await t.put(f"/api/v1/classroom/rooms/{room['id']}/layout", json=stranger)).status_code == 422


# --------------------------------------------------------------------------- unit: time-outs


def test_viewers_time_out():
    now = [100.0]
    reg = WatchRegistry(clock=lambda: now[0])
    room, user, org = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert reg.touch(room, "s1", user, "t", org, None) == (True, False)
    assert reg.is_watched(room) and reg.touch(room, "s1", user, "t", org, None) == (False, False)
    now[0] += 19
    assert reg.is_watched(room)
    now[0] += 2  # 21 s without keep-alive
    assert not reg.is_watched(room)
    assert [r for r, _ in reg.prune()] == [room] and reg.watched_rooms() == set()

    store = FrameStore()
    from smp.modules.classroom.watch import Frame
    store.put(uuid.uuid4(), "thumb", Frame(b"x", 1, 1, 1, 1, room_id=room))
    store.drop_rooms_except(set())
    assert store._frames == {}


async def test_offline_pcs_show_no_user_and_the_list_follows_moves(app, admin, monkeypatch):
    room, (pc1, pc2) = await setup_room(app, admin)
    other = (await admin.post("/api/v1/rooms", json={"name": "Lab B"})).json()
    sid = session()
    watch = lambda: admin.post(f"/api/v1/classroom/rooms/{room['id']}/watch", json={"session": sid})  # noqa: E731
    devices = (await watch()).json()["devices"]
    assert [(d["hostname"], d["user"], d["online"]) for d in devices] == [("LabA-PC1", "diak1", True), ("LabA-PC2", "diak2", True)]

    # a PC moves to another room (e.g. a laptop on another VLAN): the open page sees it without reloading
    await admin.patch(f"/api/v1/devices/{devices[1]['id']}", json={"room_id": other["id"]})
    assert [d["hostname"] for d in (await watch()).json()["devices"]] == ["LabA-PC1"]

    # nobody logged in any more
    await pc1.heartbeat(user=None)
    assert (await watch()).json()["devices"][0]["user"] is None

    # switched off: no "logged in" user for a PC that doesn't report
    await pc1.heartbeat(user="diak01")
    import smp.modules.classroom.api as classroom_api

    monkeypatch.setattr(classroom_api, "ONLINE_WINDOW", timedelta(0))
    d = (await watch()).json()["devices"][0]
    assert d["online"] is False and d["user"] is None
    assert (await admin.get(f"/api/v1/classroom/rooms/{room['id']}")).json()["devices"][0]["user"] is None
