# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Agent endpoints (mounted at /api/v1/agent).

For agents (signed requests, see crypto.py; no user session involved):
    POST /join            "manage me": creates/refreshes a join request
    POST /join/status     pending | approved | unknown (rejected/expired: join again)
    POST /heartbeat       report state, get the config; long-polls while unchanged

For admins and managers (core.devices.write; a manager only inside their buildings):
    GET  /join-requests                       pending machines
    POST /join-requests/{id}/approve          -> a new Device, or a new identity for one
    POST /join-requests/{id}/reject           remembered: it can't ask again (key or MAC)
    GET  /join-requests/rejected              those remembered rejections
    POST /join-requests/{id}/allow            forget a rejection
    POST /devices/{id}/reset-identity         forget the key: the agent must join again
    GET  /download                            version, size, SHA-256 of the offered agent
    GET  /install-command?building_id=        the install command with the building preset

A join request may carry the building the installer named (JoinIn.building: an id or a
name). It is resolved to a building; that building's managers see and approve the
request, but only into rooms of their buildings. Requests without a (matching) building
are for those with the school-wide permission.

For anyone (no sign-in), outside /api:
    GET  /downloads/smp-agent.exe             the Windows agent (see download.py)

Automatic updates: the heartbeat config's "agent" section carries "update" (version,
size, SHA-256, download path of the build for the agent's platform) unless the school
turned them off (agent settings). The agent installs it when the version differs
from its own: the server is the source of truth, also after it went back to an older
version. See agent/smp-agent/src/capabilities/update.rs.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smp.core import db
from smp.core.config import get_settings
from smp.core.context import Ctx, client_ip, require
from smp.core.dashboard import ONLINE_WINDOW
from smp.core.errors import AppError
from smp.core.events import Event
from smp.core.models import AuditLog, Building, Device, DeviceStatus, ModuleSetting, Organization, Room
from smp.core.policy import emit_policy_changed, room_for_ip
from smp.modules.agent import crypto, download
from smp.modules.agent.models import AgentJoinRequest

router = APIRouter()
public = APIRouter()  # outside /api/v1/agent: the download link

WRITE = "core.devices.write"
JOIN_TTL = timedelta(days=7)
# A rejected machine is remembered (by key and by MAC) this long after it last asked, so
# it doesn't come back with a new request every few seconds or after an IP change.
# "Allow again" in Devices forgets it at once.
REJECT_TTL = timedelta(days=90)
# Pending requests per network (one IPv4 address; an IPv6 /64, which a single machine
# can fill with addresses) and in total: a flood can't crowd out the school's own PCs.
MAX_PENDING_PER_NETWORK = 20
MAX_PENDING_TOTAL = 500
_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$")
RoomMode = Literal["fixed", "network"]


def _module(request: Request):
    return request.app.state.registry.get("agent")


def _unauthorized(why: str) -> AppError:
    # One answer for every failure: no hints about which keys exist.
    return AppError(status.HTTP_401_UNAUTHORIZED, "agent.unauthorized", f"agent request rejected: {why}")


async def _verify(request: Request, public_key_b64: str, body: bytes) -> None:
    """Check the signature headers against `public_key_b64` (raises 401)."""
    parsed = crypto.parse_public_key(public_key_b64)
    if parsed is None:
        raise _unauthorized("bad key")
    ts = request.headers.get("x-smp-time", "")
    sig = request.headers.get("x-smp-signature", "")
    try:
        skew = abs(time.time() - int(ts))
    except ValueError:
        raise _unauthorized("bad time") from None
    if skew > crypto.MAX_SKEW_SECONDS:
        raise _unauthorized("clock skew")
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not crypto.verify(parsed[0], sig, crypto.signed_message(request.method, path, ts, body)):
        raise _unauthorized("bad signature")
    if not _module(request).replay.first_time(sig):
        raise _unauthorized("replayed")


def _json(body: bytes, model: type[BaseModel]) -> Any:
    try:
        return model.model_validate_json(body or b"{}")
    except ValidationError as e:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "agent.bad_request", f"invalid body: {e.error_count()} error(s)") from e


def _server_time() -> str:
    return datetime.now(UTC).isoformat()


async def _default_org(s: AsyncSession, slug: str | None) -> Organization:
    org = (
        await s.execute(select(Organization).where(Organization.slug == (slug or get_settings().default_org)))
    ).scalar_one_or_none()
    if org is None:
        raise AppError(status.HTTP_503_SERVICE_UNAVAILABLE, "agent.not_ready", "the school isn't set up yet")
    return org


def _audit(s: AsyncSession, org_id, action: str, request: Request, target: Any = None, details: dict | None = None,
           actor: str = "agent", building_ids: list[uuid.UUID] | None = None) -> None:
    s.add(AuditLog(
        org_id=org_id, actor_type="agent", actor_name=actor, action=action, building_ids=building_ids,
        target_type=type(target).__name__.lower() if target is not None else None,
        target_id=str(target.id) if target is not None else None,
        details=details or {}, ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:400] or None,
    ))


# --------------------------------------------------------------------------- addresses
#
# Two addresses per agent:
#   reported  what the agent says its address is: the one its system uses to reach SMP
#             (agent/smp-agent/src/core/presence.rs). Right also behind a NAT, e.g. a lab
#             with its own router, where every PC would otherwise look like the router.
#   observed  where SMP sees the request come from (client_ip: nginx passes it on).
# The device's IP (Device.last_ip) is the reported one; the observed one is kept too
# (Device.observed_ip). Limits that must not be dodged (pending join requests per
# address) use the observed one: a reported address is only the agent's word.


def reported_address(value: str | None) -> str | None:
    """The agent's own address, normalized, or None if missing or not a usable address."""
    if not value:
        return None
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if addr.is_unspecified or addr.is_multicast:
        return None
    return str(addr)


def normalize_mac(value: str | None) -> str | None:
    """aa:bb:cc:dd:ee:ff, or None for nothing / not a MAC / all zeros or all ones."""
    mac = (value or "").strip().lower().replace("-", ":")
    if not _MAC_RE.match(mac) or mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return None
    return mac


def network_of(ip: str | None) -> str | None:
    """What counts as one sender for the join limits: an IPv4 address, or an IPv6 /64
    (one machine gets a whole /64 and can use any address in it)."""
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:  # ::ffff:10.0.0.5 is the IPv4 address 10.0.0.5
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


async def network_room(s: AsyncSession, org_id: uuid.UUID, reported: str | None, observed: str | None) -> Room | None:
    """The room for a "by network" device: whose subnet holds the agent's own address,
    else the address SMP sees (a NATted lab can be set up either by its inner subnet or
    by its router's address; two labs reusing the same inner range: by router address)."""
    for ip in (reported, observed):
        if ip and (room := await room_for_ip(s, org_id, ip)) is not None:
            return room
    return None


def _agent_online(device: Device) -> bool:
    """The device has an agent key that reported within the online window (dashboard.py)."""
    return bool(device.agent_id) and device.last_seen_at is not None and \
        device.last_seen_at >= datetime.now(UTC) - ONLINE_WINDOW


# --------------------------------------------------------------------------- join


class JoinIn(BaseModel):
    public_key: str = Field(max_length=200)
    hostname: str = Field(min_length=1, max_length=255)
    mac: str | None = Field(default=None, max_length=17)
    os_name: str | None = Field(default=None, max_length=64)
    os_version: str | None = Field(default=None, max_length=64)
    agent_version: str | None = Field(default=None, max_length=32)
    org: str | None = None
    address: str | None = Field(default=None, max_length=64)  # the machine's own IP (see above)
    # Which building the PC is in (a building's id or name; the installer's --building).
    # Only a hint for who sees the request: whoever approves it still picks the room.
    building: str | None = Field(default=None, max_length=200)


class JoinOut(BaseModel):
    # rejected: an admin said no; the agent waits (asking again changes nothing)
    status: Literal["pending", "approved", "rejected", "unknown"]
    fingerprint: str
    fingerprint_display: str
    server_time: str


async def resolve_building(s: AsyncSession, org_id: uuid.UUID, hint: str | None) -> uuid.UUID | None:
    """The building a hint names: its id, else its name (case-insensitive, exact), else
    None. A name that fits two buildings only by case is taken as written, or not at all."""
    if not hint:
        return None
    try:
        wanted = uuid.UUID(hint)
    except ValueError:
        pass
    else:
        if (found := (await s.execute(select(Building.id).where(Building.org_id == org_id, Building.id == wanted))).scalar_one_or_none()) is not None:
            return found
    rows = (await s.execute(select(Building.id, Building.name).where(
        Building.org_id == org_id, func.lower(Building.name) == hint.lower()))).all()
    if len(rows) == 1:
        return rows[0][0]
    return next((b for b, name in rows if name == hint), None)


@router.post("/join", response_model=JoinOut)
async def join(request: Request):
    """Signed with the key being registered, proving the agent holds it."""
    raw = await request.body()
    body: JoinIn = _json(raw, JoinIn)
    parsed = crypto.parse_public_key(body.public_key)
    if parsed is None:
        raise _unauthorized("bad key")
    fp = crypto.fingerprint(parsed[1])
    if request.headers.get("x-smp-key") != fp:
        raise _unauthorized("key mismatch")
    await _verify(request, body.public_key, raw)
    ip = client_ip(request)
    now = datetime.now(UTC)
    async with db.sessionmaker()() as s:
        org = await _default_org(s, body.org)
        if (await s.execute(select(Device.id).where(Device.org_id == org.id, Device.agent_id == fp))).first():
            return JoinOut(status="approved", fingerprint=fp, fingerprint_display=crypto.display_fingerprint(fp), server_time=_server_time())
        await s.execute(AgentJoinRequest.__table__.delete().where(AgentJoinRequest.expires_at <= now))
        mac = normalize_mac(body.mac)
        req = (
            await s.execute(select(AgentJoinRequest).where(AgentJoinRequest.org_id == org.id, AgentJoinRequest.fingerprint == fp))
        ).scalar_one_or_none()
        # Rejected before: by this key, or this machine (MAC) with a new key (reinstalled).
        rejected = req if req is not None and req.rejected_at is not None else None
        if rejected is None and req is None and mac:
            rejected = (await s.execute(select(AgentJoinRequest).where(
                AgentJoinRequest.org_id == org.id, AgentJoinRequest.mac == mac, AgentJoinRequest.rejected_at.is_not(None),
            ).limit(1))).scalar_one_or_none()
        if rejected is not None:
            rejected.expires_at = now + REJECT_TTL  # remembered while it keeps asking
            await s.commit()
            return JoinOut(status="rejected", fingerprint=fp, fingerprint_display=crypto.display_fingerprint(fp),
                           server_time=_server_time())
        hint = (body.building or "").strip() or None
        building_id = await resolve_building(s, org.id, hint)
        if req is None:
            pending = AgentJoinRequest.rejected_at.is_(None)
            if mac:  # one request per machine: a reinstalled PC's new request replaces its old one
                await s.execute(AgentJoinRequest.__table__.delete().where(
                    AgentJoinRequest.org_id == org.id, AgentJoinRequest.mac == mac, pending))
            # Don't let anyone flood the admins' approval list. Counted by where the
            # requests come from (what a script can't fake), not by key or MAC (it can).
            ips = (await s.execute(select(AgentJoinRequest.ip).where(AgentJoinRequest.org_id == org.id, pending))).scalars().all()
            here = network_of(ip)
            if sum(1 for other in ips if network_of(other) == here) >= MAX_PENDING_PER_NETWORK or len(ips) >= MAX_PENDING_TOTAL:
                raise AppError(status.HTTP_429_TOO_MANY_REQUESTS, "agent.too_many_join_requests", "too many pending join requests")
            req = AgentJoinRequest(org_id=org.id, fingerprint=fp, public_key=body.public_key)
            s.add(req)
            _audit(s, org.id, "agent.join.request", request, details={"hostname": body.hostname, "fingerprint": fp},
                   building_ids=[building_id] if building_id else None)
        req.hostname, req.mac, req.ip = body.hostname, mac, ip  # ip: observed (the limit above)
        req.reported_ip = reported_address(body.address)
        req.os_name, req.os_version, req.agent_version = body.os_name, body.os_version, body.agent_version
        if hint is not None:  # an agent that sends none (older, or told nothing) leaves what it said before
            req.building_hint, req.building_id = hint, building_id
        req.expires_at = now + JOIN_TTL
        await s.commit()
    return JoinOut(status="pending", fingerprint=fp, fingerprint_display=crypto.display_fingerprint(fp), server_time=_server_time())


@router.post("/join/status", response_model=JoinOut)
async def join_status(request: Request):
    raw = await request.body()
    fp = request.headers.get("x-smp-key", "")
    async with db.sessionmaker()() as s:
        device = (await s.execute(select(Device).where(Device.agent_id == fp, Device.agent_public_key.is_not(None)))).scalar_one_or_none()
        req = None
        if device is None:
            req = (
                await s.execute(
                    select(AgentJoinRequest).where(AgentJoinRequest.fingerprint == fp, AgentJoinRequest.expires_at > datetime.now(UTC))
                )
            ).scalar_one_or_none()
    key = device.agent_public_key if device else (req.public_key if req else None)
    if key is None:
        # Expired, forgotten ("allow again") or never asked: the agent should join (again).
        return JoinOut(status="unknown", fingerprint=fp, fingerprint_display=crypto.display_fingerprint(fp), server_time=_server_time())
    await _verify(request, key, raw)
    state = "approved" if device else ("rejected" if req is not None and req.rejected_at is not None else "pending")
    return JoinOut(status=state, fingerprint=fp, fingerprint_display=crypto.display_fingerprint(fp), server_time=_server_time())


# --------------------------------------------------------------------------- agent auth (shared)


@dataclass(frozen=True)
class AgentDevice:
    """An authenticated agent's device, detached from any DB session."""

    id: uuid.UUID
    org_id: uuid.UUID
    room_id: uuid.UUID | None
    hostname: str


async def authenticate_agent(request: Request) -> tuple[AgentDevice, bytes]:
    """For other modules' agent endpoints (e.g. classroom frame uploads): verify the
    signature of an approved device's request. Returns the device and the raw body."""
    raw = await request.body()
    fp = request.headers.get("x-smp-key", "")
    async with db.sessionmaker()() as s:
        device = (await s.execute(select(Device).where(Device.agent_id == fp, Device.agent_public_key.is_not(None)))).scalar_one_or_none()
        if device is None or device.status == DeviceStatus.RETIRED:
            raise _unauthorized("unknown device")
        key = device.agent_public_key
        found = AgentDevice(id=device.id, org_id=device.org_id, room_id=device.room_id, hostname=device.hostname)
    await _verify(request, key, raw)
    return found, raw


# --------------------------------------------------------------------------- heartbeat


class HeartbeatIn(BaseModel):
    hostname: str | None = Field(default=None, max_length=255)
    agent_version: str | None = Field(default=None, max_length=32)
    os_name: str | None = Field(default=None, max_length=64)
    os_version: str | None = Field(default=None, max_length=64)
    user: str | None = Field(default=None, max_length=256)  # console session user, None = nobody
    config_version: str | None = None  # the version the agent already has
    wait: bool = False  # long-poll: hold the answer until the config changes
    platform: str | None = Field(default=None, max_length=32)  # e.g. "windows-x86_64": which build updates it
    # The machine's own IP (the one its system uses to reach SMP); see reported_address().
    address: str | None = Field(default=None, max_length=64)
    # What this agent can do (its capabilities' names); None from agents before 1.0.4.
    capabilities: list[Annotated[str, Field(max_length=32)]] | None = Field(default=None, max_length=32)


class HeartbeatOut(BaseModel):
    device_id: str
    config: dict[str, Any]
    config_version: str
    server_time: str


async def update_offer(s: AsyncSession, org_id: uuid.UUID, platform: str | None) -> dict[str, Any] | None:
    """The agent build this server offers for `platform`, if the school wants automatic updates."""
    # TODO(evaluate, issue #17): whatever this returns, every PC installs and runs as
    # SYSTEM within minutes; a compromised server can push anything. Options: offer a new
    # version only after an admin approved it (audited), or to a test room/group first
    # (staged rollout); on the agent: signed builds, a minimum version.
    from smp.modules.agent import AgentSettings

    row = await s.get(ModuleSetting, (org_id, "agent"))
    settings = AgentSettings.model_validate(row.data if row else {})
    if not settings.auto_update:
        return None
    build = await run_in_threadpool(download.current_build, platform)
    if build is None or build.version is None:
        return None
    return {"version": build.version, "size": build.size, "sha256": build.sha256, "url": build.url,
            "spread_s": settings.update_spread_s}


async def agent_config(registry, s: AsyncSession, device: Device, platform: str | None = None) -> tuple[dict[str, Any], str]:
    """Every module's section of this device's config, and its version (a hash)."""
    config: dict[str, Any] = {"agent": {"heartbeat_wait_s": get_settings().agent_longpoll_seconds}}
    if (offer := await update_offer(s, device.org_id, platform)) is not None:
        config["agent"]["update"] = offer
    for m in registry:
        if (section := await m.agent_config(s, device)) is not None:
            config[m.name] = section
    version = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return config, version


@router.post("/heartbeat", response_model=HeartbeatOut)
async def heartbeat(request: Request):
    """Report the device's state and get its config. With wait=true and an unchanged
    config_version the answer is held until something changes or the long-poll time
    runs out. No database connection is held while waiting."""
    raw = await request.body()
    fp = request.headers.get("x-smp-key", "")
    body: HeartbeatIn = _json(raw, HeartbeatIn)
    mod = _module(request)
    registry, bus = request.app.state.registry, request.app.state.bus
    observed = client_ip(request)
    reported = reported_address(body.address)
    room_changed: tuple[uuid.UUID | None, uuid.UUID | None] | None = None

    async with db.sessionmaker()() as s:
        device = (await s.execute(select(Device).where(Device.agent_id == fp, Device.agent_public_key.is_not(None)))).scalar_one_or_none()
        if device is None or device.status == DeviceStatus.RETIRED:
            raise _unauthorized("unknown device")
        await _verify(request, device.agent_public_key, raw)
        device.last_seen_at = datetime.now(UTC)
        device.last_ip = reported or observed  # older agents don't report theirs
        device.observed_ip = observed
        device.logged_in_user = body.user or None
        if body.capabilities is not None:
            device.agent_capabilities = sorted(set(body.capabilities))
        for field in ("hostname", "agent_version", "os_name", "os_version"):
            if (value := getattr(body, field)) is not None:
                setattr(device, field, value)
        if device.room_mode == "network" and (reported or observed):
            room = await network_room(s, device.org_id, reported, observed)
            new_room = room.id if room else None
            if new_room != device.room_id:
                room_changed = (device.room_id, new_room)
                device.room_id = new_room
        gen = mod.hub.generation
        config, version = await agent_config(registry, s, device, body.platform)
        device_id, org_id = device.id, device.org_id
        await s.commit()

    if room_changed is not None:  # the laptop moved: other room, other policies (DNS, ...)
        await bus.publish(Event(subject="core.policy.changed", org_id=org_id, payload={
            "device_id": str(device_id), "old_room_id": str(room_changed[0]) if room_changed[0] else None,
            "room_id": str(room_changed[1]) if room_changed[1] else None, "reason": "device moved network",
        }))

    deadline = time.monotonic() + get_settings().agent_longpoll_seconds
    while body.wait and version == body.config_version:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not await mod.hub.wait(gen, remaining):
            break
        gen = mod.hub.generation
        async with db.sessionmaker()() as s:
            device = await s.get(Device, device_id)
            if device is None or device.agent_id != fp:
                raise _unauthorized("unknown device")  # deleted or reset while waiting
            config, version = await agent_config(registry, s, device, body.platform)

    return HeartbeatOut(device_id=str(device_id), config=config, config_version=version, server_time=_server_time())


# --------------------------------------------------------------------------- admin: approvals


class JoinRequestOut(BaseModel):
    id: uuid.UUID
    hostname: str
    mac: str | None
    ip: str | None  # the machine's own address (as its agent reports), else observed_ip
    observed_ip: str | None = None  # where SMP saw the request come from (differs behind a NAT)
    os_name: str | None
    os_version: str | None
    agent_version: str | None
    fingerprint: str
    fingerprint_display: str
    created_at: datetime
    expires_at: datetime
    # An existing device this machine probably is (same MAC, or same hostname):
    # approving "as" it gives that device the new identity (reinstalled PC).
    suggested_device_id: uuid.UUID | None = None
    # ...and that device's own agent is online right now: then this request is more
    # likely another machine pretending to be it (same hostname/MAC) than a reinstall.
    suggested_device_online: bool = False
    rejected_at: datetime | None = None
    # The building the installer named, as typed, and the building it matched.
    building_hint: str | None = None
    building_id: uuid.UUID | None = None
    building_name: str | None = None


class InstallCommandOut(BaseModel):
    command: str  # for the PC, as administrator
    building_id: uuid.UUID | None  # None: no building preset (school-wide callers only)
    building_name: str | None
    server: str  # this server's address as the browser reaches it


class DownloadInfo(BaseModel):
    available: bool
    url: str  # where to download it (same origin as the UI)
    version: str | None = None
    size: int | None = None  # bytes
    sha256: str | None = None


class ApproveIn(BaseModel):
    device_id: uuid.UUID | None = None  # set: give this existing device the new identity
    room_mode: RoomMode = "fixed"
    room_id: uuid.UUID | None = None
    # Required to take over a device whose own agent is online right now (see approve()).
    replace_online: bool = False


def _scope(ctx: Ctx) -> set[uuid.UUID] | None:
    """The buildings whose machines the caller handles; None: all of them, and also the
    requests and devices that have no building (school-wide permission)."""
    return ctx.principal.buildings_with(WRITE)


def _check_request(ctx: Ctx, req: AgentJoinRequest) -> None:
    """The caller may act on this request: school-wide, or it names one of their buildings."""
    ctx.check(WRITE, building_id=req.building_id)


@router.get("/join-requests", response_model=list[JoinRequestOut])
async def list_join_requests(ctx: Ctx = Depends(require(WRITE, any_room=True))):
    """Machines waiting for approval (managers: those that named one of their buildings)."""
    return await _join_requests(ctx, rejected=False)


@router.get("/join-requests/rejected", response_model=list[JoinRequestOut])
async def list_rejected(ctx: Ctx = Depends(require(WRITE, any_room=True))):
    """Machines an admin rejected: they can't ask again (also not with a new key from the
    same MAC) until "allow again" or REJECT_TTL after they last tried."""
    return await _join_requests(ctx, rejected=True)


async def _join_requests(ctx: Ctx, rejected: bool) -> list[JoinRequestOut]:
    now = datetime.now(UTC)
    state = AgentJoinRequest.rejected_at.is_not(None) if rejected else AgentJoinRequest.rejected_at.is_(None)
    stmt = ctx.select(AgentJoinRequest).where(AgentJoinRequest.expires_at > now, state)
    buildings = _scope(ctx)
    rooms = ctx.principal.rooms_with(WRITE)  # the same scope, as rooms: the devices a request may point at
    if buildings is not None:
        stmt = stmt.where(AgentJoinRequest.building_id.in_(buildings))
    names = dict((await ctx.db.execute(select(Building.id, Building.name).where(Building.org_id == ctx.org_id))).all())
    out = []
    for r in (await ctx.db.execute(stmt.order_by(AgentJoinRequest.created_at))).scalars():
        stmt = ctx.select(Device).where(func.lower(Device.hostname) == r.hostname.lower())
        if r.mac:  # a MAC match is the better hint (hostnames get reused)
            stmt = ctx.select(Device).where(
                or_(func.lower(Device.hostname) == r.hostname.lower(), Device.mac == r.mac.lower())
            ).order_by((Device.mac == r.mac.lower()).desc())
        if rooms is not None:  # a manager hears nothing about devices outside their buildings
            stmt = stmt.where(Device.room_id.in_(rooms))
        match = (await ctx.db.execute(stmt.limit(1))).scalar_one_or_none()
        out.append(JoinRequestOut(
            id=r.id, hostname=r.hostname, mac=r.mac, ip=r.reported_ip or r.ip, observed_ip=r.ip,
            os_name=r.os_name, os_version=r.os_version,
            agent_version=r.agent_version, fingerprint=r.fingerprint, fingerprint_display=crypto.display_fingerprint(r.fingerprint),
            created_at=r.created_at, expires_at=r.expires_at, suggested_device_id=match.id if match else None,
            suggested_device_online=match is not None and _agent_online(match), rejected_at=r.rejected_at,
            building_hint=r.building_hint, building_id=r.building_id, building_name=names.get(r.building_id),
        ))
    return out


@router.post("/join-requests/{request_id}/approve")
async def approve(request_id: uuid.UUID, body: ApproveIn, ctx: Ctx = Depends(require(WRITE, any_room=True))) -> dict[str, str]:
    """School-wide callers approve anything. A manager approves a request that named one
    of their buildings, into a room of their buildings (a new computer, or taking over
    one of theirs), whichever way the room is chosen."""
    req = await ctx.get(AgentJoinRequest, request_id)
    _check_request(ctx, req)
    if body.room_id is not None:
        await ctx.get(Room, body.room_id)
    network: Room | None = None  # "by network": the room the computer's address belongs to
    if body.room_mode == "network" and (req.reported_ip or req.ip):
        network = await network_room(ctx.db, ctx.org_id, req.reported_ip, req.ip)
    scoped = _scope(ctx) is not None
    if scoped:
        if body.room_mode == "fixed":
            if body.room_id is None:
                raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "agent.room_required",
                               "choose a room of your building for this computer")
            ctx.check(WRITE, room_id=body.room_id)
        elif network is None or not ctx.principal.can(WRITE, room_id=network.id):
            # It would land in no room, or in somebody else's: not for a manager to decide.
            raise AppError(status.HTTP_403_FORBIDDEN, "agent.room_out_of_scope",
                           "this computer's network isn't a room of your buildings; choose a room instead",
                           permission=WRITE)
    taken = (await ctx.db.execute(ctx.select(Device).where(Device.agent_id == req.fingerprint))).scalar_one_or_none()
    if taken is not None:
        raise AppError(status.HTTP_409_CONFLICT, "agent.key_in_use", "this key already belongs to a device")
    if body.device_id is not None:
        device = await ctx.get(Device, body.device_id)
        if scoped:
            ctx.check(WRITE, room_id=device.room_id)  # a computer in no room is school-level
        old_room = device.room_id
        # A request with an online PC's hostname and MAC is easy to fake (a student's
        # laptop): approving it "as" that PC would cut off the real one and let the laptop
        # be it (its room, its screen on the monitoring page). Reinstalls happen to PCs
        # whose old agent is gone, so taking over a live one needs an explicit yes.
        if _agent_online(device) and not body.replace_online:
            raise AppError(status.HTTP_409_CONFLICT, "agent.device_online",
                           f"{device.hostname} is online with its own agent; confirm replacing it",
                           hostname=device.hostname)
    else:
        device = Device(org_id=ctx.org_id, hostname=req.hostname)
        ctx.db.add(device)
        old_room = None
    device.hostname = req.hostname
    device.mac = req.mac or device.mac
    device.os_name, device.os_version, device.agent_version = req.os_name, req.os_version, req.agent_version
    device.agent_id, device.agent_public_key = req.fingerprint, req.public_key
    device.status = DeviceStatus.ACTIVE
    device.room_mode = body.room_mode
    device.last_ip, device.observed_ip = req.reported_ip or req.ip, req.ip
    if body.room_mode == "fixed":
        device.room_id = body.room_id if body.room_id is not None else device.room_id
    elif req.reported_ip or req.ip:
        device.room_id = network.id if network else None
    await ctx.db.delete(req)
    await ctx.db.flush()
    # Its room's building (from the device itself), and the one the request named.
    building_ids = {b for b in (req.building_id, network.building_id if network else None) if b is not None}
    if device.room_id is not None and (room_building := await ctx.db.scalar(
            select(Room.building_id).where(Room.id == device.room_id))) is not None:
        building_ids.add(room_building)
    ctx.audit("agent.join.approve", device, {
        "hostname": device.hostname, "fingerprint": req.fingerprint,
        "as_existing_device": body.device_id is not None, "room_mode": body.room_mode,
        "replaced_online_agent": body.device_id is not None and body.replace_online,
    }, building_ids=sorted(building_ids) or None)
    ctx.emit("core.device.updated" if body.device_id else "core.device.created", {"id": str(device.id), "hostname": device.hostname})
    if device.room_id != old_room:
        emit_policy_changed(ctx, device_id=device.id, old_room_id=old_room, room_id=device.room_id)
    await ctx.commit()
    return {"device_id": str(device.id)}


@router.post("/join-requests/{request_id}/reject", status_code=204)
async def reject(request_id: uuid.UUID, ctx: Ctx = Depends(require(WRITE, any_room=True))):
    """No. The machine is remembered (key and MAC), so it doesn't come straight back
    with a new request, also not after an IP change or a reinstall."""
    req = await ctx.get(AgentJoinRequest, request_id)
    _check_request(ctx, req)
    ctx.audit("agent.join.reject", req, {"hostname": req.hostname, "fingerprint": req.fingerprint, "ip": req.ip, "mac": req.mac})
    now = datetime.now(UTC)
    req.rejected_at, req.expires_at = now, now + REJECT_TTL
    await ctx.commit()


@router.post("/join-requests/{request_id}/allow", status_code=204)
async def allow_again(request_id: uuid.UUID, ctx: Ctx = Depends(require(WRITE, any_room=True))):
    """Forget a rejection: the machine's next request shows up as a normal one."""
    req = await ctx.get(AgentJoinRequest, request_id)
    _check_request(ctx, req)
    if req.rejected_at is None:
        raise AppError(status.HTTP_409_CONFLICT, "agent.not_rejected", "this request wasn't rejected")
    ctx.audit("agent.join.allow_again", req, {"hostname": req.hostname, "fingerprint": req.fingerprint, "mac": req.mac})
    await ctx.db.delete(req)
    await ctx.commit()


@router.get("/download", response_model=DownloadInfo)
async def download_info(ctx: Ctx = Depends(require(WRITE, any_room=True))):
    """What the Devices page shows next to the download link."""
    build = await run_in_threadpool(download.current_build)
    if build is None:
        return DownloadInfo(available=False, url=download.URL_PATH)
    return DownloadInfo(available=True, url=download.URL_PATH, version=build.version, size=build.size,
                        sha256=build.sha256)


@router.get("/install-command", response_model=InstallCommandOut)
async def install_command(request: Request, building_id: uuid.UUID | None = None,
                          ctx: Ctx = Depends(require(WRITE, any_room=True))):
    """The command to run on a PC (as administrator) so that its join request names the
    building. A manager gets their building (no choice needed if they have only one, and
    only their own are allowed); school-wide callers may name any building or none."""
    mine = _scope(ctx)
    if mine is not None:
        if building_id is None:
            if len(mine) != 1:
                raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "agent.building_required",
                               "choose the building the computer is in")
            (building_id,) = mine
        ctx.check(WRITE, building_id=building_id)
    building = await ctx.get(Building, building_id) if building_id is not None else None
    server = str(request.base_url).rstrip("/")  # scheme and host as the proxy in front passed them on
    command = f"smp-agent.exe install --server {server}"
    if building is not None:
        command += f" --building {building.id}"
    return InstallCommandOut(command=command, building_id=building.id if building else None,
                             building_name=building.name if building else None, server=server)


@public.get("/downloads/{file_name}", include_in_schema=False)
async def download_agent(file_name: str):
    """The agent (/downloads/smp-agent.exe for Windows). Public on purpose: see download.py."""
    platform = download.platform_for_file(file_name)
    build = await run_in_threadpool(download.current_build, platform.name) if platform else None
    if build is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "agent.no_download",
                       "this SMP server has no agent build (the Docker image builds it)")
    return FileResponse(build.path, media_type=build.platform.media_type, filename=build.platform.file,
                        headers={"Cache-Control": "no-cache", "X-Checksum-SHA256": build.sha256})


@router.post("/devices/{device_id}/reset-identity", status_code=204)
async def reset_identity(device_id: uuid.UUID, ctx: Ctx = Depends(require(WRITE, any_room=True))):
    """Forget the device's key (e.g. a stolen disk or a broken install). Its agent is
    refused from now on and has to join again; approve it as this same device. Managers:
    only computers in their buildings."""
    device = await ctx.get(Device, device_id)
    ctx.check(WRITE, room_id=device.room_id)
    ctx.audit("agent.identity.reset", device, {"hostname": device.hostname, "fingerprint": device.agent_id})
    device.agent_id = device.agent_public_key = None
    device.status = DeviceStatus.PENDING
    ctx.emit("core.device.updated", {"id": str(device.id), "hostname": device.hostname})
    await ctx.commit()
