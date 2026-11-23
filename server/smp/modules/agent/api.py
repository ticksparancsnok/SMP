# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Agent endpoints (mounted at /api/v1/agent).

For agents (signed requests, see crypto.py; no user session involved):
    POST /join            "manage me": creates/refreshes a join request
    POST /join/status     pending | approved | unknown (rejected/expired: join again)
    POST /heartbeat       report state, get the config; long-polls while unchanged

For admins (core.devices.write):
    GET  /join-requests                       pending machines
    POST /join-requests/{id}/approve          -> a new Device, or a new identity for one
    POST /join-requests/{id}/reject
    POST /devices/{id}/reset-identity         forget the key: the agent must join again
    GET  /download                            version, size, SHA-256 of the offered agent

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
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smp.core import db
from smp.core.config import get_settings
from smp.core.context import Ctx, client_ip, require
from smp.core.errors import AppError
from smp.core.events import Event
from smp.core.models import AuditLog, Device, DeviceStatus, ModuleSetting, Organization, Room
from smp.core.policy import emit_policy_changed, room_for_ip
from smp.modules.agent import crypto, download
from smp.modules.agent.models import AgentJoinRequest

router = APIRouter()
public = APIRouter()  # outside /api/v1/agent: the download link

WRITE = "core.devices.write"
JOIN_TTL = timedelta(days=7)
MAX_PENDING_PER_IP = 20
MAX_PENDING_TOTAL = 500
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
           actor: str = "agent") -> None:
    s.add(AuditLog(
        org_id=org_id, actor_type="agent", actor_name=actor, action=action,
        target_type=type(target).__name__.lower() if target is not None else None,
        target_id=str(target.id) if target is not None else None,
        details=details or {}, ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:400] or None,
    ))


# --------------------------------------------------------------------------- join


class JoinIn(BaseModel):
    public_key: str = Field(max_length=200)
    hostname: str = Field(min_length=1, max_length=255)
    mac: str | None = Field(default=None, max_length=17)
    os_name: str | None = Field(default=None, max_length=64)
    os_version: str | None = Field(default=None, max_length=64)
    agent_version: str | None = Field(default=None, max_length=32)
    org: str | None = None


class JoinOut(BaseModel):
    status: Literal["pending", "approved", "unknown"]
    fingerprint: str
    fingerprint_display: str
    server_time: str


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
        req = (
            await s.execute(select(AgentJoinRequest).where(AgentJoinRequest.org_id == org.id, AgentJoinRequest.fingerprint == fp))
        ).scalar_one_or_none()
        if req is None:
            # Don't let anyone flood the admins' approval list.
            per_ip = await s.scalar(select(func.count()).where(AgentJoinRequest.org_id == org.id, AgentJoinRequest.ip == ip))
            total = await s.scalar(select(func.count()).where(AgentJoinRequest.org_id == org.id))
            if (per_ip or 0) >= MAX_PENDING_PER_IP or (total or 0) >= MAX_PENDING_TOTAL:
                raise AppError(status.HTTP_429_TOO_MANY_REQUESTS, "agent.too_many_join_requests", "too many pending join requests")
            req = AgentJoinRequest(org_id=org.id, fingerprint=fp, public_key=body.public_key)
            s.add(req)
            _audit(s, org.id, "agent.join.request", request, details={"hostname": body.hostname, "fingerprint": fp})
        req.hostname, req.mac, req.ip = body.hostname, body.mac, ip
        req.os_name, req.os_version, req.agent_version = body.os_name, body.os_version, body.agent_version
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
        # Rejected, expired or never asked: the agent should join (again).
        return JoinOut(status="unknown", fingerprint=fp, fingerprint_display=crypto.display_fingerprint(fp), server_time=_server_time())
    await _verify(request, key, raw)
    state = "approved" if device else "pending"
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


class HeartbeatOut(BaseModel):
    device_id: str
    config: dict[str, Any]
    config_version: str
    server_time: str


async def update_offer(s: AsyncSession, org_id: uuid.UUID, platform: str | None) -> dict[str, Any] | None:
    """The agent build this server offers for `platform`, if the school wants automatic updates."""
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
    ip = client_ip(request)
    room_changed: tuple[uuid.UUID | None, uuid.UUID | None] | None = None

    async with db.sessionmaker()() as s:
        device = (await s.execute(select(Device).where(Device.agent_id == fp, Device.agent_public_key.is_not(None)))).scalar_one_or_none()
        if device is None or device.status == DeviceStatus.RETIRED:
            raise _unauthorized("unknown device")
        await _verify(request, device.agent_public_key, raw)
        device.last_seen_at = datetime.now(UTC)
        device.last_ip = ip
        device.logged_in_user = body.user or None
        for field in ("hostname", "agent_version", "os_name", "os_version"):
            if (value := getattr(body, field)) is not None:
                setattr(device, field, value)
        if device.room_mode == "network" and ip:
            room = await room_for_ip(s, device.org_id, ip)
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
    ip: str | None
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


@router.get("/join-requests", response_model=list[JoinRequestOut])
async def list_join_requests(ctx: Ctx = Depends(require(WRITE))):
    now = datetime.now(UTC)
    rows = (
        await ctx.db.execute(
            ctx.select(AgentJoinRequest).where(AgentJoinRequest.expires_at > now).order_by(AgentJoinRequest.created_at)
        )
    ).scalars()
    out = []
    for r in rows:
        stmt = ctx.select(Device).where(func.lower(Device.hostname) == r.hostname.lower())
        if r.mac:  # a MAC match is the better hint (hostnames get reused)
            stmt = ctx.select(Device).where(
                or_(func.lower(Device.hostname) == r.hostname.lower(), Device.mac == r.mac.lower())
            ).order_by((Device.mac == r.mac.lower()).desc())
        match = (await ctx.db.execute(stmt.limit(1))).scalar_one_or_none()
        out.append(JoinRequestOut(
            id=r.id, hostname=r.hostname, mac=r.mac, ip=r.ip, os_name=r.os_name, os_version=r.os_version,
            agent_version=r.agent_version, fingerprint=r.fingerprint, fingerprint_display=crypto.display_fingerprint(r.fingerprint),
            created_at=r.created_at, expires_at=r.expires_at, suggested_device_id=match.id if match else None,
        ))
    return out


@router.post("/join-requests/{request_id}/approve")
async def approve(request_id: uuid.UUID, body: ApproveIn, ctx: Ctx = Depends(require(WRITE))) -> dict[str, str]:
    req = await ctx.get(AgentJoinRequest, request_id)
    if body.room_id is not None:
        await ctx.get(Room, body.room_id)
    taken = (await ctx.db.execute(ctx.select(Device).where(Device.agent_id == req.fingerprint))).scalar_one_or_none()
    if taken is not None:
        raise AppError(status.HTTP_409_CONFLICT, "agent.key_in_use", "this key already belongs to a device")
    if body.device_id is not None:
        device = await ctx.get(Device, body.device_id)
        old_room = device.room_id
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
    if body.room_mode == "fixed":
        device.room_id = body.room_id if body.room_id is not None else device.room_id
    elif req.ip:
        room = await room_for_ip(ctx.db, ctx.org_id, req.ip)
        device.room_id = room.id if room else None
    await ctx.db.delete(req)
    await ctx.db.flush()
    ctx.audit("agent.join.approve", device, {
        "hostname": device.hostname, "fingerprint": req.fingerprint,
        "as_existing_device": body.device_id is not None, "room_mode": body.room_mode,
    })
    ctx.emit("core.device.updated" if body.device_id else "core.device.created", {"id": str(device.id), "hostname": device.hostname})
    if device.room_id != old_room:
        emit_policy_changed(ctx, device_id=device.id, old_room_id=old_room, room_id=device.room_id)
    await ctx.commit()
    return {"device_id": str(device.id)}


@router.post("/join-requests/{request_id}/reject", status_code=204)
async def reject(request_id: uuid.UUID, ctx: Ctx = Depends(require(WRITE))):
    req = await ctx.get(AgentJoinRequest, request_id)
    ctx.audit("agent.join.reject", req, {"hostname": req.hostname, "fingerprint": req.fingerprint, "ip": req.ip})
    await ctx.db.delete(req)
    await ctx.commit()


@router.get("/download", response_model=DownloadInfo)
async def download_info(ctx: Ctx = Depends(require(WRITE))):
    """What the Devices page shows next to the download link."""
    build = await run_in_threadpool(download.current_build)
    if build is None:
        return DownloadInfo(available=False, url=download.URL_PATH)
    return DownloadInfo(available=True, url=download.URL_PATH, version=build.version, size=build.size,
                        sha256=build.sha256)


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
async def reset_identity(device_id: uuid.UUID, ctx: Ctx = Depends(require(WRITE))):
    """Forget the device's key (e.g. a stolen disk or a broken install). Its agent is
    refused from now on and has to join again; approve it as this same device."""
    device = await ctx.get(Device, device_id)
    ctx.audit("agent.identity.reset", device, {"hostname": device.hostname, "fingerprint": device.agent_id})
    device.agent_id = device.agent_public_key = None
    device.status = DeviceStatus.PENDING
    ctx.emit("core.device.updated", {"id": str(device.id), "hostname": device.hostname})
    await ctx.commit()
