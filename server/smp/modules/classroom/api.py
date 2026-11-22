# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Classroom endpoints (mounted at /api/v1/classroom).

Agents:   PUT  /frames                      latest screen of a PC (signed, JPEG body)
Viewers:  GET  /rooms                       rooms the caller may watch
          GET  /rooms/{id}                  room, layout, PCs, current parameters
          POST /rooms/{id}/watch            start / keep watching (every few seconds)
          DELETE /rooms/{id}/watch          stop watching
          GET  /devices/{id}/frame          the latest JPEG (thumb | detail)
Editors:  PUT  /rooms/{id}/layout           auto order or seating-plan grid

Every viewing session is audited (start, stop or time-out, which PC was enlarged).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from smp.core import db
from smp.core.context import Ctx, require
from smp.core.dashboard import ONLINE_WINDOW
from smp.core.errors import AppError
from smp.core.events import Event
from smp.core.models import AuditLog, Device, Room
from smp.modules.agent.api import authenticate_agent
from smp.modules.classroom import screen_params
from smp.modules.classroom.models import ClassroomLayout
from smp.modules.classroom.watch import Frame

router = APIRouter()
log = logging.getLogger(__name__)

VIEW = "classroom.screens.view"
EDIT = "classroom.layout.edit"
MAX_FRAME_BYTES = 3 * 1024 * 1024


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


class RoomView(BaseModel):
    id: uuid.UUID
    name: str
    layout: LayoutModel
    devices: list[ScreenDevice]
    can_edit_layout: bool
    params: dict  # interval_s, thumb_width, ..., enabled


async def _room(ctx: Ctx, room_id: uuid.UUID, perm: str = VIEW) -> Room:
    room = await ctx.get(Room, room_id)
    ctx.check(perm, room.id)
    return room


async def _devices(ctx: Ctx, room_id: uuid.UUID) -> list[Device]:
    return list((await ctx.db.execute(
        ctx.select(Device).where(Device.room_id == room_id, Device.agent_id.is_not(None)).order_by(Device.hostname)
    )).scalars())


def _screen_device(d: Device) -> ScreenDevice:
    online = d.last_seen_at is not None and d.last_seen_at >= datetime.now(UTC) - ONLINE_WINDOW
    # The last reported user only means something while the PC still reports.
    return ScreenDevice(id=d.id, hostname=d.hostname, display_name=d.display_name,
                        user=d.logged_in_user if online else None, online=online)


@router.get("/rooms/{room_id}", response_model=RoomView)
async def get_room(room_id: uuid.UUID, ctx: Ctx = Depends(require(VIEW, any_room=True))):
    room = await _room(ctx, room_id)
    row = (await ctx.db.execute(ctx.select(ClassroomLayout).where(ClassroomLayout.room_id == room.id))).scalar_one_or_none()
    layout = LayoutModel() if row is None else LayoutModel(mode=row.mode, rows=row.rows, cols=row.cols,
                                                          positions={uuid.UUID(k): tuple(v) for k, v in row.positions.items()})
    return RoomView(
        id=room.id, name=room.name, layout=layout,
        devices=[_screen_device(d) for d in await _devices(ctx, room.id)],
        can_edit_layout=ctx.principal.can(EDIT, room.id),
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
    return WatchOut(
        devices=[WatchDevice(**_screen_device(d).model_dump(),
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

