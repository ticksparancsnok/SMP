# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Device agents: join requests, approval, signed heartbeats, long-poll.

FakeAgent signs requests exactly as described in smp/modules/agent/crypto.py; it's
also the reference the Rust agent's signing is checked against."""

import asyncio
import base64
import hashlib
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from smp.core.config import get_settings
from smp.modules.agent.crypto import signed_message
from tests.conftest import login_as


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeAgent:
    def __init__(self, app, ip: str = "10.20.0.5", hostname: str = "LAB1-PC01", mac: str = "aa:bb:cc:00:00:01"):
        self.app, self.ip, self.hostname, self.mac = app, ip, hostname, mac
        self.key = ec.generate_private_key(ec.SECP256R1())
        point = self.key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        self.public_key = b64url(point)
        self.fingerprint = hashlib.sha256(point).hexdigest()
        self.config_version: str | None = None

    def headers(self, path: str, raw: bytes, ts: int | None = None, key: ec.EllipticCurvePrivateKey | None = None) -> dict:
        ts_s = str(int(time.time()) if ts is None else ts)
        der = (key or self.key).sign(signed_message("POST", path, ts_s, raw), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return {
            "x-smp-key": self.fingerprint, "x-smp-time": ts_s,
            "x-smp-signature": b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big")),
            "content-type": "application/json",
        }

    async def post(self, path: str, body: dict, **kw) -> httpx.Response:
        raw = json.dumps(body).encode()
        headers = kw.pop("headers", None) or self.headers(path, raw, **kw)
        transport = httpx.ASGITransport(app=self.app, client=(self.ip, 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.post(path, content=raw, headers=headers)

    async def join(self) -> httpx.Response:
        return await self.post("/api/v1/agent/join", {
            "public_key": self.public_key, "hostname": self.hostname, "mac": self.mac,
            "os_name": "Windows 11 Pro", "os_version": "10.0.26100", "agent_version": "0.1.0",
        })

    async def status(self) -> str:
        return (await self.post("/api/v1/agent/join/status", {})).json()["status"]

    async def heartbeat(self, user: str | None = "diak01", wait: bool = False) -> httpx.Response:
        r = await self.post("/api/v1/agent/heartbeat", {
            "hostname": self.hostname, "user": user, "agent_version": "0.1.0",
            "config_version": self.config_version, "wait": wait,
        })
        if r.status_code == 200:
            self.config_version = r.json()["config_version"]
        return r


async def pending(admin) -> list[dict]:
    r = await admin.get("/api/v1/agent/join-requests")
    assert r.status_code == 200, r.text
    return r.json()


async def approve(admin, agent: FakeAgent, **body) -> str:
    (req,) = [r for r in await pending(admin) if r["fingerprint"] == agent.fingerprint]
    r = await admin.post(f"/api/v1/agent/join-requests/{req['id']}/approve", json=body)
    assert r.status_code == 200, r.text
    return r.json()["device_id"]


# --------------------------------------------------------------------------- join and approval


async def test_join_approve_heartbeat(app, admin):
    agent = FakeAgent(app)
    r = await agent.join()
    assert r.status_code == 200 and r.json()["status"] == "pending"
    shown = r.json()["fingerprint_display"]  # what the PC's installer shows
    head = agent.fingerprint[:20].upper()
    assert shown == "-".join(head[i : i + 4] for i in range(0, 20, 4))  # e.g. 3F2A-91C4-0B7E-55D1-AA20
    assert await agent.status() == "pending"
    assert (await agent.heartbeat()).status_code == 401  # not approved yet

    (req,) = await pending(admin)
    assert req["hostname"] == "LAB1-PC01" and req["fingerprint_display"] == shown and req["ip"] == "10.20.0.5"
    room = (await admin.post("/api/v1/rooms", json={"name": "Lab 1"})).json()
    device_id = await approve(admin, agent, room_mode="fixed", room_id=room["id"])
    assert await pending(admin) == [] and await agent.status() == "approved"

    agent.ip = "10.99.0.7"  # a new IP changes nothing: the key is the identity
    r = await agent.heartbeat(user="AD\\kiss.peter")
    assert r.status_code == 200, r.text
    assert r.json()["device_id"] == device_id and r.json()["config"]["agent"]["heartbeat_wait_s"] > 0
    d = (await admin.get(f"/api/v1/devices/{device_id}")).json()
    assert d["agent_id"] == agent.fingerprint and d["status"] == "active" and d["room_id"] == room["id"]
    assert d["last_ip"] == "10.99.0.7" and d["logged_in_user"] == "AD\\kiss.peter" and d["os_name"] == "Windows 11 Pro"
    assert d["last_seen_at"]

    # joining again with an approved key just says so
    assert (await agent.join()).json()["status"] == "approved"
    actions = {e["action"] for e in (await admin.get("/api/v1/audit", params={"action": "agent."})).json()}
    assert {"agent.join.request", "agent.join.approve"} <= actions


async def test_reject_and_rejoin(app, admin):
    agent = FakeAgent(app)
    await agent.join()
    (req,) = await pending(admin)
    assert (await admin.post(f"/api/v1/agent/join-requests/{req['id']}/reject")).status_code == 204
    assert await agent.status() == "unknown"  # rejected: the agent may ask again ...
    assert (await agent.join()).json()["status"] == "pending"  # ... and lands in the list again
    assert len(await pending(admin)) == 1


async def test_join_requests_are_rate_limited(app, admin, monkeypatch):
    monkeypatch.setattr("smp.modules.agent.api.MAX_PENDING_PER_IP", 2)
    codes = [(await FakeAgent(app, ip="10.30.0.9").join()).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert (await FakeAgent(app, ip="10.30.0.10").join()).status_code == 200  # other machines aren't blocked


async def test_only_admins_approve(app, admin):
    agent = FakeAgent(app)
    await agent.join()
    (req,) = await pending(admin)
    async with login_as(app, admin, "agent.teacher", [{"role": "teacher"}]) as teacher:
        assert (await teacher.get("/api/v1/agent/join-requests")).status_code == 403
        assert (await teacher.post(f"/api/v1/agent/join-requests/{req['id']}/approve", json={})).status_code == 403


# --------------------------------------------------------------------------- signatures


async def test_signatures_are_enforced(app, admin):
    agent = FakeAgent(app)
    await agent.join()
    await approve(admin, agent)
    path = "/api/v1/agent/heartbeat"
    raw = json.dumps({"user": "x"}).encode()

    tampered = agent.headers(path, raw)  # signed one body, sends another
    assert (await agent.post(path, {"user": "admin"}, headers=tampered)).status_code == 401

    good = agent.headers(path, raw)
    transport = httpx.ASGITransport(app=app, client=(agent.ip, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post(path, content=raw, headers=good)).status_code == 200
        assert (await c.post(path, content=raw, headers=good)).status_code == 401  # replay

    old = agent.headers(path, raw, ts=int(time.time()) - 600)  # outside the allowed clock skew
    assert (await agent.post(path, {"user": "x"}, headers=old)).status_code == 401

    impostor = ec.generate_private_key(ec.SECP256R1())  # someone else's key, our fingerprint
    forged = agent.headers(path, raw, key=impostor)
    r = await agent.post(path, {"user": "x"}, headers=forged)
    assert r.status_code == 401 and r.json()["code"] == "agent.unauthorized"

    stranger = FakeAgent(app)  # never joined
    assert (await stranger.heartbeat()).status_code == 401

    # agent signatures are no way into the user API
    r = await agent.post("/api/v1/users", {})
    assert r.status_code in (401, 405)
    transport = httpx.ASGITransport(app=app, client=(agent.ip, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/v1/users", headers=agent.headers("/api/v1/users", b""))).status_code == 401


async def test_join_must_be_signed_by_the_registered_key(app, admin):
    agent = FakeAgent(app)
    body = {"public_key": agent.public_key, "hostname": "X"}
    raw = json.dumps(body).encode()
    other = ec.generate_private_key(ec.SECP256R1())
    assert (await agent.post("/api/v1/agent/join", body, headers=agent.headers("/api/v1/agent/join", raw, key=other))).status_code == 401
    assert await pending(admin) == []


# --------------------------------------------------------------------------- rooms


async def test_room_follows_network_for_roaming_devices(app, admin, testpol):
    lab = (await admin.post("/api/v1/rooms", json={"name": "Lab", "subnets": ["10.40.1.0/24"]})).json()
    library = (await admin.post("/api/v1/rooms", json={"name": "Library", "subnets": ["10.40.2.0/24"]})).json()
    laptop = FakeAgent(app, ip="10.40.1.20", hostname="LAPTOP-07")
    await laptop.join()
    device_id = await approve(admin, laptop, room_mode="network")
    assert (await admin.get(f"/api/v1/devices/{device_id}")).json()["room_id"] == lab["id"]  # from the join IP

    testpol.received.clear()
    laptop.ip = "10.40.2.31"  # walked to the library (other AP / VLAN)
    assert (await laptop.heartbeat()).status_code == 200
    assert (await admin.get(f"/api/v1/devices/{device_id}")).json()["room_id"] == library["id"]
    moved = [e for e in testpol.received if e.subject == "core.policy.changed"]
    assert moved and moved[-1].payload["room_id"] == library["id"]  # DNS etc. follow the device

    laptop.ip = "192.168.50.3"  # somewhere SMP has no room for
    await laptop.heartbeat()
    assert (await admin.get(f"/api/v1/devices/{device_id}")).json()["room_id"] is None

    desktop = FakeAgent(app, ip="10.40.1.21", hostname="LAB-PC02", mac="aa:bb:cc:00:00:02")
    await desktop.join()
    desktop_id = await approve(admin, desktop, room_mode="fixed", room_id=library["id"])
    desktop.ip = "10.40.1.99"
    await desktop.heartbeat()
    assert (await admin.get(f"/api/v1/devices/{desktop_id}")).json()["room_id"] == library["id"]  # fixed stays


# --------------------------------------------------------------------------- reinstall


async def test_reset_identity_and_rejoin_as_the_same_device(app, admin):
    old = FakeAgent(app, hostname="LAB1-PC05", mac="aa:bb:cc:00:00:05")
    await old.join()
    device_id = await approve(admin, old)
    assert (await admin.post(f"/api/v1/agent/devices/{device_id}/reset-identity")).status_code == 204
    assert (await old.heartbeat()).status_code == 401

    new = FakeAgent(app, hostname="LAB1-PC05", mac="aa:bb:cc:00:00:05")  # reinstalled: new key
    await new.join()
    (req,) = await pending(admin)
    assert req["suggested_device_id"] == device_id  # same MAC: probably the same PC
    assert await approve(admin, new, device_id=device_id) == device_id
    assert (await new.heartbeat()).status_code == 200
    assert (await old.heartbeat()).status_code == 401
    d = (await admin.get(f"/api/v1/devices/{device_id}")).json()
    assert d["agent_id"] == new.fingerprint and d["status"] == "active"


async def test_a_key_belongs_to_one_device(app, admin):
    agent = FakeAgent(app)
    await agent.join()
    await approve(admin, agent)
    # a second request with the same key can't exist (join says "approved"), and
    # approving a stale request for a key in use is refused
    assert (await agent.join()).json()["status"] == "approved"
    assert await pending(admin) == []


# --------------------------------------------------------------------------- long-poll


async def test_heartbeat_long_poll(app, admin, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_longpoll_seconds", 1.5)
    marker = {"value": 1}
    real = __import__("smp.modules.agent.api", fromlist=["agent_config"]).agent_config

    async def config_with_marker(registry, s, device):
        config, _ = await real(registry, s, device)
        config["test"] = dict(marker)
        version = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
        return config, version

    monkeypatch.setattr("smp.modules.agent.api.agent_config", config_with_marker)
    agent = FakeAgent(app)
    await agent.join()
    await approve(admin, agent)
    first = await agent.heartbeat()
    assert first.json()["config"]["test"] == {"value": 1}

    # unchanged config: the answer is held for the long-poll time, then comes back the same
    t0 = time.monotonic()
    r = await agent.heartbeat(wait=True)
    held = time.monotonic() - t0
    assert r.status_code == 200 and r.json()["config_version"] == first.json()["config_version"] and 1.3 < held < 3

    # a change while waiting answers right away with the new config
    hub = app.state.registry.get("agent").hub

    async def change_soon():
        await asyncio.sleep(0.2)
        marker["value"] = 2
        hub.notify()

    t0 = time.monotonic()
    r, _ = await asyncio.gather(agent.heartbeat(wait=True), change_soon())
    assert r.json()["config"]["test"] == {"value": 2} and time.monotonic() - t0 < 1.0


@pytest.mark.parametrize("subject", ["core.policy.changed", "core.settings.changed", "core.device.updated"])
async def test_changes_wake_agents(app, subject):
    from smp.core.events import Event

    hub = app.state.registry.get("agent").hub
    gen = hub.generation
    org_id = __import__("uuid").uuid4()
    await app.state.bus.publish(Event(subject=subject, org_id=org_id, payload={}))
    await asyncio.sleep(0.05)  # handlers run as tasks
    assert hub.generation > gen


# --------------------------------------------------------------------------- the agent download


async def test_agent_download(app, admin, monkeypatch, tmp_path):
    anyone = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")  # not signed in
    monkeypatch.setattr(get_settings(), "agent_download_dir", str(tmp_path))
    # nothing built (e.g. a development server)
    assert (await anyone.get("/downloads/smp-agent.exe")).json()["code"] == "agent.no_download"
    assert (await admin.get("/api/v1/agent/download")).json() == {
        "available": False, "url": "/downloads/smp-agent.exe", "version": None, "size": None, "sha256": None}

    exe = b"MZ" + b"\x90" * 1000  # starts like a Windows program; content doesn't matter
    (tmp_path / "smp-agent.exe").write_bytes(exe)
    (tmp_path / "smp-agent.version").write_text("0.1.0\n")
    r = await anyone.get("/downloads/smp-agent.exe")  # no sign-in needed: it's taken to the PCs
    assert r.status_code == 200 and r.content == exe
    assert 'filename="smp-agent.exe"' in r.headers["content-disposition"]
    assert r.headers["x-checksum-sha256"] == hashlib.sha256(exe).hexdigest()

    info = (await admin.get("/api/v1/agent/download")).json()
    assert info == {"available": True, "url": "/downloads/smp-agent.exe", "version": "0.1.0", "size": len(exe),
                    "sha256": hashlib.sha256(exe).hexdigest()}
    async with login_as(app, admin, "teacher.dl", [{"role": "teacher"}]) as t:
        assert (await t.get("/api/v1/agent/download")).status_code == 403  # the info is for device admins
    await anyone.aclose()
