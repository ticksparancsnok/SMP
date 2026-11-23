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
    def __init__(self, app, ip: str = "10.20.0.5", hostname: str = "LAB1-PC01", mac: str = "aa:bb:cc:00:00:01",
                 address: str | None = None):
        # ip: where the server sees requests come from. address: what the agent reports as
        # its own (None: an agent from before it reported one).
        self.app, self.ip, self.hostname, self.mac, self.address = app, ip, hostname, mac, address
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
            **({"address": self.address} if self.address else {}),
        })

    async def status(self) -> str:
        return (await self.post("/api/v1/agent/join/status", {})).json()["status"]

    async def heartbeat(self, user: str | None = "diak01", wait: bool = False, platform: str | None = None) -> httpx.Response:
        r = await self.post("/api/v1/agent/heartbeat", {
            "hostname": self.hostname, "user": user, "agent_version": "0.1.0",
            "config_version": self.config_version, "wait": wait, "platform": platform,
            **({"address": self.address} if self.address else {}),
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


async def test_a_rejected_machine_stays_rejected_until_allowed_again(app, admin):
    agent = FakeAgent(app, mac="AA-BB-CC-00-00-01")  # however the MAC is written
    await agent.join()
    (req,) = await pending(admin)
    assert (await admin.post(f"/api/v1/agent/join-requests/{req['id']}/reject")).status_code == 204
    assert await agent.status() == "rejected" and await pending(admin) == []
    # Asking again changes nothing: not with the same key, not after an IP change...
    agent.ip = "10.20.0.77"
    assert (await agent.join()).json()["status"] == "rejected"
    # ...and not reinstalled with a new key on the same machine (same MAC).
    reinstalled = FakeAgent(app, ip="10.20.0.78", mac="aa:bb:cc:00:00:01")
    assert (await reinstalled.join()).json()["status"] == "rejected"
    assert await pending(admin) == []
    (gone,) = (await admin.get("/api/v1/agent/join-requests/rejected")).json()
    assert gone["hostname"] == agent.hostname and gone["rejected_at"]

    # "Allow again": the machine's next request is a normal one.
    assert (await admin.post(f"/api/v1/agent/join-requests/{gone['id']}/allow")).status_code == 204
    assert await agent.status() == "unknown"
    assert (await agent.join()).json()["status"] == "pending" and len(await pending(admin)) == 1
    actions = [e["action"] for e in (await admin.get("/api/v1/audit", params={"action": "agent.join."})).json()]
    assert {"agent.join.reject", "agent.join.allow_again"} <= set(actions)


async def test_a_reinstalled_pc_replaces_its_own_request(app, admin):
    first = FakeAgent(app, mac="aa:bb:cc:00:00:31")
    await first.join()
    again = FakeAgent(app, mac="aa:bb:cc:00:00:31")  # new key, same machine
    await again.join()
    assert [r["fingerprint"] for r in await pending(admin)] == [again.fingerprint]


async def test_join_requests_are_rate_limited(app, admin, monkeypatch):
    monkeypatch.setattr("smp.modules.agent.api.MAX_PENDING_PER_NETWORK", 2)
    codes = [(await FakeAgent(app, ip="10.30.0.9", mac=f"aa:bb:cc:30:00:{i:02x}").join()).status_code for i in range(3)]
    assert codes == [200, 200, 429]
    assert (await FakeAgent(app, ip="10.30.0.10", mac="aa:bb:cc:30:01:00").join()).status_code == 200  # others aren't blocked


async def test_one_laptop_cant_flood_the_queue_with_ipv6_addresses(app, admin, monkeypatch):
    """A machine owns a whole IPv6 /64 and could send each request from a new address,
    with a new key and a made-up MAC: the limit counts the /64."""
    monkeypatch.setattr("smp.modules.agent.api.MAX_PENDING_PER_NETWORK", 3)
    codes = [(await FakeAgent(app, ip=f"2001:db8:5:6::{i + 1:x}", mac=f"02:00:00:00:00:{i:02x}").join()).status_code
             for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    other_net = FakeAgent(app, ip="2001:db8:5:7::1", mac="02:00:00:00:01:00")  # another /64: another machine
    assert (await other_net.join()).status_code == 200


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

    async def config_with_marker(registry, s, device, platform=None):
        config, _ = await real(registry, s, device, platform)
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


async def test_heartbeat_offers_the_build_for_the_agents_platform(app, admin, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "agent_download_dir", str(tmp_path))
    agent = FakeAgent(app, hostname="UPD-PC01", mac="aa:bb:cc:00:00:51")
    await agent.join()
    await approve(admin, agent)
    # no build yet: nothing offered
    assert "update" not in (await agent.heartbeat()).json()["config"]["agent"]

    exe, elf = b"MZ" + b"\x90" * 100, b"\x7fELF" + b"\x00" * 100
    (tmp_path / "smp-agent.exe").write_bytes(exe)
    (tmp_path / "smp-agent.version").write_text("0.2.0 A\n")
    r = (await agent.heartbeat()).json()  # Windows is the default platform
    assert r["config"]["agent"]["update"] == {"version": "0.2.0 A", "size": len(exe), "sha256": hashlib.sha256(exe).hexdigest(),
                                              "url": "/downloads/smp-agent.exe", "spread_s": 300}
    before = r["config_version"]
    assert "update" not in (await agent.heartbeat(platform="linux-x86_64")).json()["config"]["agent"]  # no Linux build

    (tmp_path / "smp-agent-linux-x86_64").write_bytes(elf)
    (tmp_path / "smp-agent-linux-x86_64.version").write_text("0.2.0 A\n")
    offer = (await agent.heartbeat(platform="linux-x86_64")).json()["config"]["agent"]["update"]
    assert offer["url"] == "/downloads/smp-agent-linux-x86_64" and offer["sha256"] == hashlib.sha256(elf).hexdigest()
    anyone = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    got = await anyone.get(offer["url"])
    assert got.status_code == 200 and got.content == elf
    assert (await anyone.get("/downloads/not-an-agent")).status_code == 404
    await anyone.aclose()
    assert "update" not in (await agent.heartbeat(platform="plan9-mips")).json()["config"]["agent"]

    # turned off by the school: no offer, and the config (hash) changes so waiting agents hear it
    r = await admin.put("/api/v1/modules/agent/settings", json={"auto_update": False, "update_spread_s": 0})
    assert r.status_code == 200, r.text
    off = (await agent.heartbeat()).json()
    assert "update" not in off["config"]["agent"] and off["config_version"] != before
    await admin.put("/api/v1/modules/agent/settings", json={"auto_update": True, "update_spread_s": 0})
    assert (await agent.heartbeat()).json()["config"]["agent"]["update"]["spread_s"] == 0


async def test_update_settings_are_validated(admin):
    r = await admin.put("/api/v1/modules/agent/settings", json={"auto_update": True, "update_spread_s": 99999})
    assert r.status_code == 422


def test_the_offered_build_follows_file_changes(monkeypatch, tmp_path):
    """Replacing the file with one of the same size and modification time (copies keep it,
    sometimes only to the second), or changing only the .version file, is noticed."""
    import os

    from smp.modules.agent import download

    monkeypatch.setattr(get_settings(), "agent_download_dir", str(tmp_path))
    exe, ver = tmp_path / "smp-agent.exe", tmp_path / "smp-agent.version"
    exe.write_bytes(b"MZ-one")
    ver.write_text("0.1.0\n")
    os.utime(exe, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    first = download.current_build()
    exe.write_bytes(b"MZ-two")  # same size
    os.utime(exe, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))  # same mtime
    second = download.current_build()
    assert second.sha256 != first.sha256 and second.sha256 == hashlib.sha256(b"MZ-two").hexdigest()
    ver.write_text("0.2.0\n")
    assert download.current_build().version == "0.2.0"


# --------------------------------------------------------------------------- addresses behind a NAT


async def test_agents_report_their_own_address_behind_a_nat(app, admin):
    """A lab behind its own router: the server sees the router (10.20.0.1) for every PC,
    the agents report their real addresses (192.168.50.x)."""
    inner = (await admin.post("/api/v1/rooms", json={"name": "NAT lab", "subnets": ["192.168.50.0/24"]})).json()
    outer = (await admin.post("/api/v1/rooms", json={"name": "Behind the router", "subnets": ["10.20.0.0/24"]})).json()

    pc = FakeAgent(app, ip="10.20.0.1", hostname="NATLAB-PC01", mac="aa:bb:cc:50:00:01", address="192.168.50.11")
    await pc.join()
    (req,) = [r for r in await pending(admin) if r["fingerprint"] == pc.fingerprint]
    assert (req["ip"], req["observed_ip"]) == ("192.168.50.11", "10.20.0.1")  # what the admin sees
    device_id = await approve(admin, pc, room_mode="network")
    device = (await admin.get(f"/api/v1/devices/{device_id}")).json()
    assert (device["last_ip"], device["observed_ip"], device["room_id"]) == ("192.168.50.11", "10.20.0.1", inner["id"])

    # The PC moves within the lab: its own address follows, the router's stays.
    pc.address = "192.168.50.42"
    assert (await pc.heartbeat()).status_code == 200
    device = (await admin.get(f"/api/v1/devices/{device_id}")).json()
    assert (device["last_ip"], device["observed_ip"]) == ("192.168.50.42", "10.20.0.1")
    found = (await admin.get("/api/v1/devices", params={"ip": "192.168.50.42"})).json()["items"]
    assert [d["id"] for d in found] == [device_id]
    via_router = (await admin.get("/api/v1/devices", params={"ip": "10.20.0.1"})).json()["items"]
    assert device_id in [d["id"] for d in via_router]  # every PC behind the router

    # An inner address no room knows: the room comes from the address the server sees.
    pc.address = "172.30.9.9"
    await pc.heartbeat()
    assert (await admin.get(f"/api/v1/devices/{device_id}")).json()["room_id"] == outer["id"]

    # Nonsense is ignored (the seen address is used), and so is an agent that sends none.
    for bad in ("0.0.0.0", "not-an-ip", "224.0.0.1"):
        pc.address = bad
        await pc.heartbeat()
        assert (await admin.get(f"/api/v1/devices/{device_id}")).json()["last_ip"] == "10.20.0.1", bad
    pc.address = None
    await pc.heartbeat()
    assert (await admin.get(f"/api/v1/devices/{device_id}")).json()["last_ip"] == "10.20.0.1"


async def test_an_online_pc_isnt_taken_over_by_mistake(app, admin):
    """A laptop asking to join with an online lab PC's hostname and MAC."""
    real = FakeAgent(app, ip="10.88.0.20", hostname="LAB-PC07", mac="aa:bb:cc:88:00:07")
    await real.join()
    real_id = await approve(admin, real)
    await real.heartbeat()
    fake = FakeAgent(app, ip="10.88.0.66", hostname="LAB-PC07", mac="aa:bb:cc:88:00:07")
    await fake.join()
    (req,) = [r for r in await pending(admin) if r["fingerprint"] == fake.fingerprint]
    assert req["suggested_device_id"] == real_id and req["suggested_device_online"] is True  # the UI warns

    r = await admin.post(f"/api/v1/agent/join-requests/{req['id']}/approve", json={"device_id": real_id})
    assert r.status_code == 409 and r.json()["code"] == "agent.device_online"
    assert (await real.heartbeat()).status_code == 200  # the real PC is untouched

    # A real reinstall: the admin says so explicitly (and it's audited).
    r = await admin.post(f"/api/v1/agent/join-requests/{req['id']}/approve", json={"device_id": real_id, "replace_online": True})
    assert r.status_code == 200
    (entry, *_) = (await admin.get("/api/v1/audit", params={"action": "agent.join.approve"})).json()
    assert entry["details"]["replaced_online_agent"] is True
