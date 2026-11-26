# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Remote control: the teacher's WebSocket, the agent's frame uploads and input polls,
who may control what, and how it ends."""

import asyncio
import json

import httpx
import pytest

from smp.modules.classroom.remote import RemoteSession, clean_events, coalesce_moves
from tests.conftest import login_as
from tests.test_agent import FakeAgent, b64url
from tests.test_classroom import JPEG, setup_room


class WsClient:
    """A minimal WebSocket client that talks ASGI to the app directly (httpx has no
    WebSockets). `closed` is set to (code, reason) once the server closes."""

    def __init__(self, app, path: str, cookie: str | None, origin: str | None = "http://test"):
        headers = [(b"host", b"test")]
        if cookie:
            headers.append((b"cookie", f"smp_session={cookie}".encode()))
        if origin:
            headers.append((b"origin", origin.encode()))
        self.scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "ws",
                      "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                      "headers": headers, "client": ("10.9.9.9", 40000), "server": ("test", 80), "subprotocols": []}
        self.app, self.to_app, self.from_app = app, asyncio.Queue(), asyncio.Queue()
        self.closed: tuple[int, str] | None = None

    async def connect(self) -> WsClient:
        self.task = asyncio.create_task(self.app(self.scope, self.to_app.get, self.from_app.put))
        await self.to_app.put({"type": "websocket.connect"})
        assert (await self.next())["type"] == "websocket.accept"
        return self

    async def next(self) -> dict:
        async with asyncio.timeout(5):  # a test waits at most this long for the server
            msg = await self.from_app.get()
        if msg["type"] == "websocket.close":
            self.closed = (msg.get("code", 1000), msg.get("reason", ""))
        return msg

    async def json(self) -> dict:
        msg = await self.next()
        assert msg["type"] == "websocket.send" and "text" in msg, msg
        return json.loads(msg["text"])

    async def send(self, data: dict) -> None:
        await self.to_app.put({"type": "websocket.receive", "text": json.dumps(data)})

    async def close_code(self) -> tuple[int, str]:
        while self.closed is None:
            await self.next()
        return self.closed

    async def leave(self) -> None:
        await self.to_app.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 5)


def cookie_of(client: httpx.AsyncClient) -> str:
    return client.cookies.get("smp_session")


async def signed(agent: FakeAgent, method: str, path: str, body: bytes = b"", headers: dict | None = None) -> httpx.Response:
    """A signed agent request of any method (FakeAgent signs POSTs only)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    from smp.modules.agent.crypto import signed_message

    h = agent.headers(path, body)
    der = agent.key.sign(signed_message(method, path, h["x-smp-time"], body), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    h["x-smp-signature"] = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    transport = httpx.ASGITransport(app=agent.app, client=(agent.ip, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, content=body, headers={**h, **(headers or {})})


async def remote_of(agent: FakeAgent) -> dict | None:
    return (await agent.heartbeat(capabilities=["classroom", "lock", "remote"])).json()["config"]["classroom"].get("remote")


async def setup(app, admin, n=1, capable=True):
    room, agents = await setup_room(app, admin, n=n)
    for a in agents:
        await a.heartbeat(capabilities=["classroom", "lock", "remote"] if capable else ["classroom"])
    devices = (await admin.get(f"/api/v1/classroom/rooms/{room['id']}")).json()["devices"]
    return room, agents, [d["id"] for d in devices]


async def test_remote_control_flow(app, admin):
    room, (pc,), (device_id,) = await setup(app, admin)
    async with login_as(app, admin, "teacher.r", [{"role": "teacher", "room_id": room["id"]}]) as t:
        view = (await t.get(f"/api/v1/classroom/rooms/{room['id']}")).json()
        assert view["can_control"] is True and view["devices"][0]["can_remote"] is True
        assert await remote_of(pc) is None

        ws = await WsClient(app, f"/api/v1/classroom/devices/{device_id}/remote", cookie_of(t)).connect()
        assert (await ws.json()) == {"type": "ready", "hostname": "LabA-PC1", "user": "diak01"}
        remote = await remote_of(pc)
        assert remote["interval_ms"] == 200 and remote["width"] == 1600 and remote["quality"] == 60
        assert remote["frame_path"] == "/api/v1/classroom/agent/remote/frame"
        controlled = (await t.get(f"/api/v1/classroom/rooms/{room['id']}")).json()["devices"][0]
        assert controlled["controlled_by"] == "teacher.r"

        # the PC's screen reaches the browser: sizes, then the JPEG
        frame = await signed(pc, "PUT", remote["frame_path"] + "?n=1", JPEG, {
            "content-type": "image/jpeg", "x-smp-remote-session": remote["session"],
            "x-smp-frame-size": "1600x900", "x-smp-screen-size": "1920x1080"})
        assert frame.status_code == 204, frame.text
        assert await ws.json() == {"type": "frame", "width": 1600, "height": 900, "screen_width": 1920, "screen_height": 1080}
        assert (await ws.next())["bytes"] == JPEG
        # a frame for another (old) session is turned away
        stale = await signed(pc, "PUT", remote["frame_path"] + "?n=2", JPEG, {"x-smp-remote-session": "old"})
        assert stale.status_code == 409 and stale.json()["code"] == "classroom.not_controlled"

        # the teacher's input reaches the PC, checked and with mouse moves merged
        await ws.send({"events": [{"t": "move", "x": 0.1, "y": 0.2}, {"t": "move", "x": 0.5, "y": 1.7},
                                  {"t": "button", "b": 0, "down": True}, {"t": "key", "code": "KeyA", "down": True},
                                  {"t": "key", "code": "rm -rf", "down": True}, {"t": "evil"}]})
        r = await signed(pc, "GET", f"{remote['input_path']}?session={remote['session']}&n=3")
        assert r.status_code == 200, r.text
        assert r.json()["events"] == [{"t": "move", "x": 0.5, "y": 1.0}, {"t": "button", "b": 0, "down": True},
                                      {"t": "key", "code": "KeyA", "down": True}]

        # blocking the student's own input: confirmed, in the PC's config, audited
        assert remote["block_local"] is False
        await ws.send({"block_input": True})
        assert await ws.json() == {"type": "block_input", "on": True}
        remote = await remote_of(pc)
        assert remote["block_local"] is True
        await ws.send({"block_input": False})
        assert await ws.json() == {"type": "block_input", "on": False}
        assert (await remote_of(pc))["block_local"] is False

        # the PC's next poll waits; closing the view ends it at once
        poll = asyncio.create_task(signed(pc, "GET", f"{remote['input_path']}?session={remote['session']}&n=4"))
        await asyncio.sleep(0.2)
        assert not poll.done()
        await ws.leave()
        assert (await asyncio.wait_for(poll, 5)).status_code == 409
        assert await remote_of(pc) is None

    entries = (await admin.get("/api/v1/audit", params={"action": "classroom.remote."})).json()
    stop = next(e for e in entries if e["action"] == "classroom.remote.stop")
    assert stop["details"]["hostname"] == "LabA-PC1" and stop["details"]["reason"] == "closed"
    assert any(e["action"] == "classroom.remote.start" for e in entries)
    toggles = [e["details"]["on"] for e in entries if e["action"] == "classroom.remote.block_input"]
    assert sorted(toggles) == [False, True]


async def test_who_may_control_what(app, admin):
    room, (pc,), (device_id,) = await setup(app, admin)
    path = f"/api/v1/classroom/devices/{device_id}/remote"

    # not signed in, or from another site's page
    assert (await (await WsClient(app, path, None).connect()).close_code())[0] == 4401
    assert (await (await WsClient(app, path, cookie_of(admin), origin="https://evil.example").connect()).close_code())[0] == 4401
    assert (await (await WsClient(app, path, cookie_of(admin), origin=None).connect()).close_code())[0] == 4401

    other = (await admin.post("/api/v1/rooms", json={"name": "Lab Z"})).json()
    async with login_as(app, admin, "teacher.z", [{"role": "teacher", "room_id": other["id"]}]) as t:
        code, reason = await (await WsClient(app, path, cookie_of(t)).connect()).close_code()
        assert (code, reason) == (4403, "permission.missing")

    # someone else controls it: busy; the same person again takes over
    first = await WsClient(app, path, cookie_of(admin)).connect()
    assert (await first.json())["type"] == "ready"
    async with login_as(app, admin, "teacher.a2", [{"role": "teacher", "room_id": room["id"]}]) as t:
        assert await (await WsClient(app, path, cookie_of(t)).connect()).close_code() == (4409, "classroom.remote_busy")
    second = await WsClient(app, path, cookie_of(admin)).connect()
    assert (await second.json())["type"] == "ready"
    assert await first.close_code() == (4000, "classroom.remote_taken_over")

    # locking ends control, and a locked PC can't be controlled
    await admin.post(f"/api/v1/classroom/rooms/{room['id']}/lock", json={})
    assert await second.close_code() == (4000, "classroom.remote_locked")
    assert await (await WsClient(app, path, cookie_of(admin)).connect()).close_code() == (4409, "classroom.remote_locked")
    assert await remote_of(pc) is None


async def test_old_agents_cant_be_controlled(app, admin):
    room, (pc,), (device_id,) = await setup(app, admin, capable=False)
    assert not (await admin.get(f"/api/v1/classroom/rooms/{room['id']}")).json()["devices"][0]["can_remote"]
    ws = await WsClient(app, f"/api/v1/classroom/devices/{device_id}/remote", cookie_of(admin)).connect()
    assert await ws.close_code() == (4426, "classroom.remote_unsupported")


async def test_input_waits_and_ends():
    s = RemoteSession(*(None,) * 6)
    assert await s.take_input(wait_s=0.05) == []  # nothing within the wait
    s.push_input([{"t": "key", "code": "KeyB", "down": True}])
    assert await s.take_input() == [{"t": "key", "code": "KeyB", "down": True}]
    waiting = asyncio.create_task(s.take_input(wait_s=5))
    await asyncio.sleep(0.05)
    s.end("closed")
    assert await asyncio.wait_for(waiting, 1) is None


@pytest.mark.parametrize("raw, expected", [
    ({"events": [{"t": "wheel", "dy": -99999}]}, [{"t": "wheel", "dy": -2400, "dx": 0}]),
    ({"events": [{"t": "wheel", "dy": 0}]}, []),
    ({"events": [{"t": "button", "b": 7, "down": True}, {"t": "button", "b": 1, "down": "yes"}]}, []),
    ({"events": [{"t": "move", "x": float("nan"), "y": 0}, {"t": "move", "x": True, "y": 0}]}, []),
    ({"events": "nope"}, []),
    ([1, 2], []),
])
def test_clean_events(raw, expected):
    assert clean_events(raw) == expected


def test_moves_are_merged():
    move = lambda x: {"t": "move", "x": x, "y": 0}  # noqa: E731
    click = {"t": "button", "b": 0, "down": True}
    assert coalesce_moves([move(1), move(2), click, move(3), move(4)]) == [move(2), click, move(4)]
