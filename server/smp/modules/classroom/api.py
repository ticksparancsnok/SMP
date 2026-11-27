# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Classroom endpoints (mounted at /api/v1/classroom).

Agents:   PUT  /frames                      latest screen of a PC (signed, JPEG body)
          GET  /agent/lock-logo             the school's lock screen logo (signed, PNG)
          PUT  /agent/remote/frame          the controlled PC's screen (signed, JPEG body)
          GET  /agent/remote/input          the teacher's input for it (signed, long poll)
Viewers:  GET  /rooms                       rooms the caller may watch
          GET  /rooms/{id}                  room, layout, PCs, current parameters
          POST /rooms/{id}/watch            start / keep watching (every few seconds)
          DELETE /rooms/{id}/watch          stop watching
          GET  /devices/{id}/frame          the latest JPEG (thumb | detail)
Editors:  PUT  /rooms/{id}/layout           auto order or seating-plan grid
Lockers:  POST /rooms/{id}/lock             lock all / some PCs of a room, with a message
          POST /rooms/{id}/unlock           unlock all / some
Control:  WS   /devices/{id}/remote         remote control of one PC (see remote.py)
Admins:   GET|POST|DELETE /lock-logo        the school's logo on locked screens (PNG)
          GET  /lock-logo/info              its size and fingerprint, or null

Every viewing session is audited (start, stop or time-out, which PC was enlarged),
and so is every lock, unlock, logo change and remote control session.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import struct
import time
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from smp.core import db
from smp.core.context import Ctx, require, websocket_ctx
from smp.core.dashboard import ONLINE_WINDOW
from smp.core.errors import AppError
from smp.core.events import Event
from smp.core.models import AuditLog, Device, Room
from smp.core.uploads import read_limited
from smp.modules.agent.api import authenticate_agent
from smp.modules.classroom import ClassroomSettings, lock_default_text, screen_params
from smp.modules.classroom.models import ClassroomLayout, ClassroomLock, ClassroomLockLogo
from smp.modules.classroom.remote import RemoteFrame, RemoteSession, RemoteSessions, clean_events
from smp.modules.classroom.watch import Frame

router = APIRouter()
log = logging.getLogger(__name__)

VIEW = "classroom.screens.view"
EDIT = "classroom.layout.edit"
LOCK = "classroom.screens.lock"
CONTROL = "classroom.screens.control"
MAX_FRAME_BYTES = 3 * 1024 * 1024
MAX_LOGO_BYTES = 1024 * 1024  # the web UI shrinks logos to 1024 px before uploading: far less
MAX_LOGO_SIDE = 2048  # the agent accepts at most this (capabilities/lock/picture.rs)


def _mod(request: Request):
    return request.app.state.registry.get("classroom")


async def _config_changed(request: Request, org_id: uuid.UUID, reason: str) -> None:
    """Watching changed: wake the agents so they start/stop sending right away."""
    mod = _mod(request)
    mod.frames.drop_rooms_except(mod.watches.watched_rooms())
    await request.app.state.bus.publish(Event(subject="agent.config.changed", org_id=org_id, payload={"reason": reason}))


# --------------------------------------------------------------------------- time-outs

_pruner: asyncio.Task | None = None


def _ensure_pruner(request: Request) -> None:
    """Viewers that vanish (closed tab, sleeping laptop) time out; this notices it,
    stops the PCs' uploads and writes the audit entry."""
    global _pruner
    if _pruner is not None and not _pruner.done():
        return
    mod, bus = _mod(request), request.app.state.bus

    async def loop() -> None:
        while True:
            await asyncio.sleep(5)
            try:
                gone = mod.watches.prune()
                if not gone:
                    continue
                mod.frames.drop_rooms_except(mod.watches.watched_rooms())
                async with db.sessionmaker()() as s:
                    for room_id, v in gone:
                        s.add(AuditLog(org_id=v.org_id, actor_type="user", actor_id=str(v.user_id), actor_name=v.user_name,
                                       action="classroom.watch.stop", target_type="room", target_id=str(room_id),
                                       details={"reason": "timeout"}))
                    await s.commit()
                for org_id in {v.org_id for _, v in gone}:
                    await bus.publish(Event(subject="agent.config.changed", org_id=org_id, payload={"reason": "viewer timed out"}))
            except Exception:  # noqa: BLE001 - keep pruning whatever happens
                log.exception("classroom watch pruner")

    _pruner = asyncio.get_running_loop().create_task(loop())


# --------------------------------------------------------------------------- agents: frames


@router.put("/frames", status_code=204, include_in_schema=False)
async def put_frame(request: Request):
    """An agent's latest screen. Accepted only while its room is watched (and, for a
    full-size frame, while someone has it enlarged)."""
    device, body = await authenticate_agent(request)
    mod = _mod(request)
    kind = request.headers.get("x-smp-frame-kind", "thumb")
    if kind not in ("thumb", "detail"):
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "classroom.bad_frame", "unknown frame kind")
    if not mod.watches.is_watched(device.room_id) or (kind == "detail" and device.id not in mod.watches.detail_devices(device.room_id)):
        raise AppError(status.HTTP_409_CONFLICT, "classroom.not_watched", "nobody is watching this screen")
    if request.headers.get("x-smp-frame-unchanged") == "1":
        # Same screen as last time (a test sheet, a document): a few hundred bytes
        # instead of the JPEG again. The browser keeps its cached copy too.
        if not mod.frames.confirm(device.id, kind, device.room_id):
            raise AppError(status.HTTP_409_CONFLICT, "classroom.need_frame", "send the full frame")
        return
    if len(body) > MAX_FRAME_BYTES or not body.startswith(b"\xff\xd8"):
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "classroom.bad_frame", "expected a JPEG of at most 3 MB")

    def size(header: str) -> tuple[int, int]:
        try:
            w, h = (int(x) for x in request.headers.get(header, "0x0").lower().split("x"))
            return max(w, 0), max(h, 0)
        except ValueError:
            return 0, 0

    w, h = size("x-smp-frame-size")
    sw, sh = size("x-smp-screen-size")
    mod.frames.put(device.id, kind, Frame(jpeg=body, width=w, height=h, screen_width=sw or w, screen_height=sh or h,
                                          room_id=device.room_id))


# --------------------------------------------------------------------------- viewers


class RoomSummary(BaseModel):
    id: uuid.UUID
    name: str
    devices: int
    online: int


@router.get("/rooms", response_model=list[RoomSummary])
async def list_rooms(ctx: Ctx = Depends(require(VIEW, any_room=True))):
    allowed = ctx.principal.rooms_with(VIEW)
    stmt = ctx.select(Room).order_by(Room.name)
    if allowed is not None:
        stmt = stmt.where(Room.id.in_(allowed))
    since = datetime.now(UTC) - ONLINE_WINDOW
    out = []
    for room in (await ctx.db.execute(stmt)).scalars():
        total = await ctx.db.scalar(select(func.count()).where(Device.org_id == ctx.org_id, Device.room_id == room.id,
                                                               Device.agent_id.is_not(None)))
        online = await ctx.db.scalar(select(func.count()).where(Device.org_id == ctx.org_id, Device.room_id == room.id,
                                                                Device.agent_id.is_not(None), Device.last_seen_at >= since))
        out.append(RoomSummary(id=room.id, name=room.name, devices=total or 0, online=online or 0))
    return out


class LayoutModel(BaseModel):
    mode: Literal["auto", "grid"] = "auto"
    rows: int = Field(4, ge=1, le=20)
    cols: int = Field(6, ge=1, le=20)
    positions: dict[uuid.UUID, tuple[int, int]] = {}  # device id -> (row, col), 0-based


class ScreenDevice(BaseModel):
    id: uuid.UUID
    hostname: str
    display_name: str
    user: str | None
    online: bool
    locked: bool = False  # screen lock on (the PC shows it once it gets the config)
    can_remote: bool = False  # its agent can be remote controlled (reports the "remote" capability)
    controlled_by: str | None = None  # who controls it right now


class RoomView(BaseModel):
    id: uuid.UUID
    name: str
    layout: LayoutModel
    devices: list[ScreenDevice]
    can_edit_layout: bool
    can_lock: bool
    can_control: bool
    lock_message: str  # the school's suggested lock text (settings), to start the lock dialog with
    lock_default: str  # what locked PCs show without a message (block page language)
    params: dict  # interval_s, thumb_width, ..., enabled


async def _room(ctx: Ctx, room_id: uuid.UUID, perm: str = VIEW) -> Room:
    room = await ctx.get(Room, room_id)
    ctx.check(perm, room.id)
    return room


async def _devices(ctx: Ctx, room_id: uuid.UUID) -> list[Device]:
    return list((await ctx.db.execute(
        ctx.select(Device).where(Device.room_id == room_id, Device.agent_id.is_not(None)).order_by(Device.hostname)
    )).scalars())


async def _locked(ctx: Ctx, room_id: uuid.UUID) -> set[uuid.UUID]:
    """The room's locked PCs (locks of PCs that left the room don't count; see ClassroomLock)."""
    return set((await ctx.db.execute(
        select(ClassroomLock.device_id).join(Device, Device.id == ClassroomLock.device_id)
        .where(ClassroomLock.org_id == ctx.org_id, ClassroomLock.room_id == room_id, Device.room_id == room_id)
    )).scalars())


def _screen_device(d: Device, locked: set[uuid.UUID], controllers: dict[uuid.UUID, str]) -> ScreenDevice:
    online = d.last_seen_at is not None and d.last_seen_at >= datetime.now(UTC) - ONLINE_WINDOW
    # The last reported user only means something while the PC still reports.
    return ScreenDevice(id=d.id, hostname=d.hostname, display_name=d.display_name,
                        user=d.logged_in_user if online else None, online=online, locked=d.id in locked,
                        can_remote="remote" in (d.agent_capabilities or []), controlled_by=controllers.get(d.id))


@router.get("/rooms/{room_id}", response_model=RoomView)
async def get_room(room_id: uuid.UUID, ctx: Ctx = Depends(require(VIEW, any_room=True))):
    room = await _room(ctx, room_id)
    row = (await ctx.db.execute(ctx.select(ClassroomLayout).where(ClassroomLayout.room_id == room.id))).scalar_one_or_none()
    layout = LayoutModel() if row is None else LayoutModel(mode=row.mode, rows=row.rows, cols=row.cols,
                                                          positions={uuid.UUID(k): tuple(v) for k, v in row.positions.items()})
    locked = await _locked(ctx, room.id)
    settings = await ctx.settings("classroom")
    assert isinstance(settings, ClassroomSettings)
    return RoomView(
        id=room.id, name=room.name, layout=layout,
        devices=[_screen_device(d, locked, _mod(ctx.request).remote.controllers()) for d in await _devices(ctx, room.id)],
        can_edit_layout=ctx.principal.can(EDIT, room.id),
        can_lock=ctx.principal.can(LOCK, room.id),
        can_control=ctx.principal.can(CONTROL, room.id),
        lock_message=settings.lock_message,
        lock_default=await lock_default_text(ctx.db, ctx.org_id),
        params=await screen_params(ctx.db, ctx.org_id, room_id=room.id),
    )


class WatchIn(BaseModel):
    session: str = Field(min_length=8, max_length=64)  # random id per open page
    detail_device_id: uuid.UUID | None = None  # the PC shown enlarged, if any


class FrameInfo(BaseModel):
    at: float  # server's unix time; changes with every new picture (the UI's cache key)
    age_s: float  # seconds since the PC last confirmed it (the browser's clock may be off)
    width: int
    height: int
    screen_width: int
    screen_height: int


class WatchDevice(ScreenDevice):
    """The room's PCs as they are now (a laptop may have joined or left the room
    since the page loaded), plus their latest pictures."""

    thumb: FrameInfo | None
    detail: FrameInfo | None


class WatchOut(BaseModel):
    devices: list[WatchDevice]
    enabled: bool  # false: monitoring isn't allowed right now (policy)
    interval_s: int
    detail_interval_s: int


def _info(f: Frame | None) -> FrameInfo | None:
    if f is None:
        return None
    return FrameInfo(at=f.at, age_s=round(max(time.time() - f.seen, 0.0), 1), width=f.width, height=f.height,
                     screen_width=f.screen_width, screen_height=f.screen_height)


@router.post("/rooms/{room_id}/watch", response_model=WatchOut)
async def watch(room_id: uuid.UUID, body: WatchIn, request: Request, ctx: Ctx = Depends(require(VIEW, any_room=True))):
    room = await _room(ctx, room_id)
    devices = await _devices(ctx, room.id)
    if body.detail_device_id is not None and body.detail_device_id not in {d.id for d in devices}:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "device not in this room", entity="Device")
    mod = _mod(request)
    _ensure_pruner(request)
    new, detail_changed = mod.watches.touch(room.id, body.session, ctx.principal.id, ctx.principal.name, ctx.org_id,
                                            body.detail_device_id)
    if new:
        ctx.audit("classroom.watch.start", room, {"room": room.name})
    if detail_changed and body.detail_device_id is not None:
        d = next(d for d in devices if d.id == body.detail_device_id)
        ctx.audit("classroom.watch.detail", room, {"device_id": str(d.id), "hostname": d.hostname, "user": d.logged_in_user})
    await ctx.commit()
    if new or detail_changed:
        await _config_changed(request, ctx.org_id, "watching changed")
    params = await screen_params(ctx.db, ctx.org_id, room_id=room.id)
    locked = await _locked(ctx, room.id)
    return WatchOut(
        devices=[WatchDevice(**_screen_device(d, locked, mod.remote.controllers()).model_dump(),
                             thumb=_info(mod.frames.get(d.id, "thumb")), detail=_info(mod.frames.get(d.id, "detail")))
                 for d in devices],
        enabled=params["enabled"], interval_s=params["interval_s"], detail_interval_s=params["detail_interval_s"],
    )


@router.delete("/rooms/{room_id}/watch", status_code=204)
async def stop_watching(room_id: uuid.UUID, session: str, request: Request, ctx: Ctx = Depends(require(VIEW, any_room=True))):
    room = await _room(ctx, room_id)
    if _mod(request).watches.stop(room.id, session, ctx.principal.id) is not None:
        ctx.audit("classroom.watch.stop", room, {"reason": "closed"})
        await ctx.commit()
        await _config_changed(request, ctx.org_id, "viewer left")


@router.get("/devices/{device_id}/frame", responses={200: {"content": {"image/jpeg": {}}}})
async def get_frame(device_id: uuid.UUID, request: Request, kind: Literal["thumb", "detail"] = "thumb",
                    ctx: Ctx = Depends(require(VIEW, any_room=True))):
    device = await ctx.get(Device, device_id)
    ctx.check(VIEW, device.room_id)
    frame = _mod(request).frames.get(device.id, kind)
    if frame is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "classroom.no_frame", "no current screen", entity="Frame")
    etag = f'"{device.id.hex[:8]}-{kind}-{frame.at:.3f}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)  # the browser has it: no bytes sent
    return Response(frame.jpeg, media_type="image/jpeg", headers=headers)


# --------------------------------------------------------------------------- layout


@router.put("/rooms/{room_id}/layout", response_model=LayoutModel)
async def put_layout(room_id: uuid.UUID, body: LayoutModel, request: Request,
                     ctx: Ctx = Depends(require(EDIT, any_room=True))):
    room = await _room(ctx, room_id, EDIT)
    in_room = {d.id for d in await _devices(ctx, room.id)}
    seen: set[tuple[int, int]] = set()
    for device_id, (r, c) in body.positions.items():
        if device_id not in in_room:
            raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "classroom.bad_layout", "a placed computer isn't in this room")
        if not (0 <= r < body.rows and 0 <= c < body.cols) or (r, c) in seen:
            raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "classroom.bad_layout", "positions must be distinct cells of the grid")
        seen.add((r, c))
    row = (await ctx.db.execute(ctx.select(ClassroomLayout).where(ClassroomLayout.room_id == room.id))).scalar_one_or_none()
    if row is None:
        row = ClassroomLayout(org_id=ctx.org_id, room_id=room.id)
        ctx.db.add(row)
    row.mode, row.rows, row.cols = body.mode, body.rows, body.cols
    row.positions = {str(k): list(v) for k, v in body.positions.items()}
    ctx.audit("classroom.layout.update", room, {"mode": body.mode, "rows": body.rows, "cols": body.cols,
                                                "placed": len(body.positions)})
    await ctx.commit()
    return body



# --------------------------------------------------------------------------- screen lock


class LockIn(BaseModel):
    message: str = Field("", max_length=500)  # empty: the agent's built-in sentence
    device_ids: list[uuid.UUID] | None = None  # None: every computer in the room


class UnlockIn(BaseModel):
    device_ids: list[uuid.UUID] | None = None  # None: every computer in the room


class LockOut(BaseModel):
    locked: list[uuid.UUID]  # the room's locked computers now


def _chosen(devices: list[Device], ids: list[uuid.UUID] | None) -> list[Device]:
    if ids is None:
        return devices
    by_id = {d.id: d for d in devices}
    if missing := [i for i in ids if i not in by_id]:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", f"device {missing[0]} not in this room", entity="Device")
    return [by_id[i] for i in dict.fromkeys(ids)]


def _lock_details(devices: list[Device], **extra) -> dict:
    return {"count": len(devices), "hostnames": [d.hostname for d in devices][:100], **extra}


@router.post("/rooms/{room_id}/lock", response_model=LockOut)
async def lock_screens(room_id: uuid.UUID, body: LockIn, request: Request,
                       ctx: Ctx = Depends(require(LOCK, any_room=True))):
    """Lock the room's computers (or `device_ids`): they show `message` with the school's
    logo and block keyboard and mouse until unlocked. Locking a locked one again changes
    its message. Stays until someone unlocks it (also across server restarts), while
    the computer is in this room."""
    room = await _room(ctx, room_id, LOCK)
    devices = _chosen(await _devices(ctx, room.id), body.device_ids)
    message = body.message.replace("\r\n", "\n").strip()
    if devices:
        stmt = insert(ClassroomLock).values([
            {"id": uuid.uuid4(), "org_id": ctx.org_id, "room_id": room.id, "device_id": d.id, "message": message,
             "locked_by": ctx.principal.name}
            for d in devices
        ])
        # One lock per computer: locking again (also from another room) replaces it.
        await ctx.db.execute(stmt.on_conflict_do_update(
            index_elements=[ClassroomLock.device_id],
            set_={"room_id": stmt.excluded.room_id, "message": stmt.excluded.message,
                  "locked_by": stmt.excluded.locked_by, "updated_at": func.now()},
        ))
        ctx.audit("classroom.lock", room, _lock_details(devices, message=message))
    await ctx.commit()
    if devices:
        # A locked PC's input is blocked anyway: end remote control instead of fighting it.
        _mod(request).remote.end_devices({d.id for d in devices}, "locked")
        await _config_changed(request, ctx.org_id, "screens locked")
    return LockOut(locked=sorted(await _locked(ctx, room.id)))


@router.post("/rooms/{room_id}/unlock", response_model=LockOut)
async def unlock_screens(room_id: uuid.UUID, body: UnlockIn, request: Request,
                         ctx: Ctx = Depends(require(LOCK, any_room=True))):
    room = await _room(ctx, room_id, LOCK)
    stmt = delete(ClassroomLock).where(ClassroomLock.org_id == ctx.org_id, ClassroomLock.room_id == room.id)
    if body.device_ids is not None:
        stmt = stmt.where(ClassroomLock.device_id.in_(body.device_ids))
    unlocked = set((await ctx.db.execute(stmt.returning(ClassroomLock.device_id))).scalars())
    if unlocked:
        devices = [d for d in await _devices(ctx, room.id) if d.id in unlocked]
        ctx.audit("classroom.unlock", room, _lock_details(devices) | {"count": len(unlocked)})
    await ctx.commit()
    if unlocked:
        await _config_changed(request, ctx.org_id, "screens unlocked")
    return LockOut(locked=sorted(await _locked(ctx, room.id)))


# --------------------------------------------------------------------------- lock screen logo

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _bad_logo(why: str) -> AppError:
    return AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "classroom.bad_logo", f"the logo must be a PNG image: {why}",
                    max_side=MAX_LOGO_SIDE)


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height of a complete PNG file (from its header; the agent decodes the
    rest, and rejects anything damaged on its own)."""
    if not data.startswith(PNG_SIGNATURE) or len(data) < 45 or data[12:16] != b"IHDR":
        raise _bad_logo("not a PNG file")
    if data[-8:-4] != b"IEND":
        raise _bad_logo("the file is incomplete")
    width, height = struct.unpack(">II", data[16:24])
    if not (1 <= width <= MAX_LOGO_SIDE and 1 <= height <= MAX_LOGO_SIDE):
        raise _bad_logo(f"{width}x{height} is larger than {MAX_LOGO_SIDE}x{MAX_LOGO_SIDE}")
    return width, height


class LogoInfo(BaseModel):
    sha256: str
    size: int
    width: int
    height: int
    updated_at: datetime


async def _logo(ctx: Ctx) -> ClassroomLockLogo | None:
    return (await ctx.db.execute(ctx.select(ClassroomLockLogo))).scalar_one_or_none()


@router.get("/lock-logo/info", response_model=LogoInfo | None)
async def lock_logo_info(ctx: Ctx = Depends(require("core.settings.read"))):
    """The school's lock screen logo, or null: locked screens show the SMP logo."""
    row = await _logo(ctx)
    return None if row is None else LogoInfo(sha256=row.sha256, size=row.size, width=row.width, height=row.height,
                                             updated_at=row.updated_at)


@router.get("/lock-logo", responses={200: {"content": {"image/png": {}}}})
async def get_lock_logo(ctx: Ctx = Depends(require("core.settings.read"))):
    row = await _logo(ctx)
    if row is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "classroom.no_logo", "no lock screen logo", entity="Logo")
    return Response(row.png, media_type="image/png", headers={"Cache-Control": "private, no-cache"})


@router.post("/lock-logo", response_model=LogoInfo,
             openapi_extra={"requestBody": {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}})
async def upload_lock_logo(request: Request, ctx: Ctx = Depends(require("core.settings.write"))):
    """Replace the logo on locked screens (raw PNG body, at most 1 MB and 2048x2048).
    Locked computers switch to it right away."""
    data = await read_limited(request, MAX_LOGO_BYTES)
    width, height = png_size(data)
    sha256 = hashlib.sha256(data).hexdigest()
    row = await _logo(ctx)
    if row is None:
        row = ClassroomLockLogo(org_id=ctx.org_id)
        ctx.db.add(row)
    row.png, row.sha256, row.size, row.width, row.height = data, sha256, len(data), width, height
    ctx.audit("classroom.lock_logo.update", None, {"size": len(data), "width": width, "height": height, "sha256": sha256},
              target_type="lock_logo")
    await ctx.commit()
    await ctx.db.refresh(row)
    await _config_changed(request, ctx.org_id, "lock screen logo changed")
    return LogoInfo(sha256=sha256, size=len(data), width=width, height=height, updated_at=row.updated_at)


@router.delete("/lock-logo", status_code=204)
async def delete_lock_logo(request: Request, ctx: Ctx = Depends(require("core.settings.write"))):
    """Back to the SMP logo on locked screens."""
    row = await _logo(ctx)
    if row is not None:
        await ctx.db.delete(row)
        ctx.audit("classroom.lock_logo.delete", None, target_type="lock_logo")
        await ctx.commit()
        await _config_changed(request, ctx.org_id, "lock screen logo removed")


@router.get("/agent/lock-logo", include_in_schema=False)
async def agent_lock_logo(request: Request):
    """For locked computers (a signed request): the logo their config names by SHA-256."""
    device, _ = await authenticate_agent(request)
    async with db.sessionmaker()() as s:
        png = (await s.execute(select(ClassroomLockLogo.png).where(ClassroomLockLogo.org_id == device.org_id))).scalar_one_or_none()
    if png is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "classroom.no_logo", "no lock screen logo", entity="Logo")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- remote control
#
# Close codes the web UI shows a message for (WebSocket codes 4000-4999 are ours).
WS_NOT_SIGNED_IN, WS_FORBIDDEN, WS_NOT_FOUND, WS_CONFLICT, WS_UNSUPPORTED, WS_ENDED = 4401, 4403, 4404, 4409, 4426, 4000
MAX_INPUT_MESSAGE = 64 * 1024


async def _refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Accept, then close with our code: a refused handshake would only show the
    browser "connection failed" (code 1006), without saying why."""
    await websocket.accept()
    await websocket.close(code=code, reason=reason)


@router.websocket("/devices/{device_id}/remote")
async def remote_control(websocket: WebSocket, device_id: uuid.UUID):
    """The teacher's side of remote control. The browser sends input as JSON text
    messages ({"events": [...]}, see remote.clean_events) and gets each new frame as a
    JSON text message (sizes) followed by a binary message (the JPEG). Closing it ends
    control."""
    mod = websocket.app.state.registry.get("classroom")
    async with db.sessionmaker()() as s:
        ctx = await websocket_ctx(websocket, s)
        if ctx is None:
            return await _refuse(websocket, WS_NOT_SIGNED_IN, "auth.not_authenticated")
        device = await s.get(Device, device_id)
        if device is None or device.org_id != ctx.org_id or device.agent_id is None or device.room_id is None:
            return await _refuse(websocket, WS_NOT_FOUND, "not_found")
        if not ctx.principal.can(CONTROL, device.room_id):
            return await _refuse(websocket, WS_FORBIDDEN, "permission.missing")
        if "remote" not in (device.agent_capabilities or []):
            return await _refuse(websocket, WS_UNSUPPORTED, "classroom.remote_unsupported")
        locked = await s.scalar(select(func.count()).where(ClassroomLock.device_id == device.id,
                                                            ClassroomLock.room_id == device.room_id))
        if locked:
            return await _refuse(websocket, WS_CONFLICT, "classroom.remote_locked")
        current = mod.remote.get(device.id)
        if current is not None and current.user_id != ctx.principal.id:
            return await _refuse(websocket, WS_CONFLICT, "classroom.remote_busy")
        # The same teacher again (a second tab, a reload): the new view takes over.
        session = RemoteSession(device.id, device.room_id, ctx.org_id, device.hostname, ctx.principal.id,
                                ctx.principal.name)
        mod.remote.start(session)
        room = await s.get(Room, device.room_id)
        ctx.audit("classroom.remote.start", room, {"device_id": str(device.id), "hostname": device.hostname,
                                                    "user": device.logged_in_user})
        await ctx.commit()
        hostname, user = device.hostname, device.logged_in_user

    await websocket.accept()
    await _remote_config_changed(websocket, ctx.org_id, "remote control started")
    await websocket.send_json({"type": "ready", "hostname": hostname, "user": user})
    tasks = [asyncio.create_task(_forward_frames(websocket, session)),
             asyncio.create_task(_receive_input(websocket, session)),
             asyncio.create_task(session.ended.wait())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        reason = session.end_reason or "closed"
        mod.remote.finish(session, reason)
        with contextlib.suppress(Exception):  # the browser may be gone already
            await websocket.close(code=WS_ENDED, reason=f"classroom.remote_{reason.replace(' ', '_')}")
        async with db.sessionmaker()() as s:
            s.add(AuditLog(org_id=session.org_id, actor_type="user", actor_id=str(session.user_id),
                           actor_name=session.user_name, action="classroom.remote.stop", target_type="room",
                           target_id=str(session.room_id), details={"device_id": str(session.device_id),
                                                                    "hostname": session.hostname, "reason": reason,
                                                                    "seconds": session.seconds()}))
            await s.commit()
        await _remote_config_changed(websocket, session.org_id, "remote control ended")


async def _remote_config_changed(websocket: WebSocket, org_id: uuid.UUID, reason: str) -> None:
    await websocket.app.state.bus.publish(Event(subject="agent.config.changed", org_id=org_id, payload={"reason": reason}))


async def _forward_frames(websocket: WebSocket, session: RemoteSession) -> None:
    """Each new frame to the browser. Only the newest counts: a slow connection gets
    fewer frames, never a growing backlog."""
    while True:
        await session.frame_ready.wait()
        session.frame_ready.clear()
        if session.ended.is_set():
            return
        f = session.frame
        if f is not None:
            await websocket.send_json({"type": "frame", "width": f.width, "height": f.height,
                                       "screen_width": f.screen_width, "screen_height": f.screen_height})
            await websocket.send_bytes(f.jpeg)


async def _receive_input(websocket: WebSocket, session: RemoteSession) -> None:
    """The browser's messages: input batches ({"events": [...]}, checked, to the PC's
    queue) and the student-input switch ({"block_input": true|false}). Returns when the
    browser goes."""
    try:
        while True:
            text = await websocket.receive_text()
            if len(text) > MAX_INPUT_MESSAGE:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict) and isinstance(message.get("block_input"), bool):
                await _set_block_input(websocket, session, message["block_input"])
            elif events := clean_events(message):
                session.push_input(events)
    except (WebSocketDisconnect, RuntimeError, KeyError):  # closed, or a binary message (not ours)
        return


async def _set_block_input(websocket: WebSocket, session: RemoteSession, on: bool) -> None:
    """Block (or allow again) the student's own keyboard and mouse: audited, sent to the
    PC with its config, and confirmed to the browser (it shows what is in effect)."""
    if on != session.block_local:
        session.block_local = on
        async with db.sessionmaker()() as s:
            s.add(AuditLog(org_id=session.org_id, actor_type="user", actor_id=str(session.user_id),
                           actor_name=session.user_name, action="classroom.remote.block_input", target_type="room",
                           target_id=str(session.room_id),
                           details={"device_id": str(session.device_id), "hostname": session.hostname, "on": on}))
            await s.commit()
        await _remote_config_changed(websocket, session.org_id, "student input blocked" if on else "student input allowed")
    await websocket.send_json({"type": "block_input", "on": session.block_local})


def _remote_session(request: Request, device_id: uuid.UUID, session_id: str | None) -> RemoteSession:
    """The PC's current session, if `session_id` is it (a PC still sending for an old
    session is turned away, like a frame nobody watches)."""
    remote: RemoteSessions = _mod(request).remote
    session = remote.get(device_id)
    if session is None or session.ended.is_set() or session.id != session_id:
        raise AppError(status.HTTP_409_CONFLICT, "classroom.not_controlled", "nobody controls this computer")
    return session


@router.put("/agent/remote/frame", status_code=204, include_in_schema=False)
async def put_remote_frame(request: Request):
    """The controlled PC's screen (with the mouse pointer). "Unchanged" costs nothing:
    the teacher simply keeps the picture they have."""
    device, body = await authenticate_agent(request)
    session = _remote_session(request, device.id, request.headers.get("x-smp-remote-session"))
    if request.headers.get("x-smp-frame-unchanged") == "1":
        return
    if len(body) > MAX_FRAME_BYTES or not body.startswith(b"\xff\xd8"):
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "classroom.bad_frame", "expected a JPEG of at most 3 MB")
    w, h = _size(request.headers.get("x-smp-frame-size"))
    sw, sh = _size(request.headers.get("x-smp-screen-size"))
    session.put_frame(RemoteFrame(jpeg=body, width=w, height=h, screen_width=sw or w, screen_height=sh or h))


@router.get("/agent/remote/input", include_in_schema=False)
async def get_remote_input(request: Request, session: str = ""):
    """The teacher's input for the controlled PC: answered at once when there is some,
    else after up to 20 s with none. 409 when the session has ended: the agent stops
    asking. No database connection is held while waiting."""
    device, _ = await authenticate_agent(request)
    events = await _remote_session(request, device.id, session).take_input()
    if events is None:
        raise AppError(status.HTTP_409_CONFLICT, "classroom.not_controlled", "nobody controls this computer")
    return {"events": events}


def _size(value: str | None) -> tuple[int, int]:
    try:
        w, h = (int(x) for x in (value or "0x0").lower().split("x"))
        return max(w, 0), max(h, 0)
    except ValueError:
        return 0, 0
