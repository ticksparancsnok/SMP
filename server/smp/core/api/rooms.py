# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

import ipaddress
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from smp.core.api import ORMModel
from smp.core.context import Ctx, require
from smp.core.models import Device, Room, TargetType
from smp.core.policy import emit_policy_changed, remove_assignments_for

router = APIRouter(prefix="/rooms", tags=["rooms"])


def _normalise_subnets(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    return [str(ipaddress.ip_network(s, strict=False)) for s in v]


class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    building: str = ""
    description: str = ""
    subnets: list[str] = []

    v_subnets = field_validator("subnets")(_normalise_subnets)


class RoomUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    building: str | None = None
    description: str | None = None
    subnets: list[str] | None = None

    v_subnets = field_validator("subnets")(_normalise_subnets)


class RoomOut(ORMModel):
    id: uuid.UUID
    name: str
    building: str
    description: str
    subnets: list[str]
    device_count: int = 0


def _out(room: Room, count: int = 0) -> RoomOut:
    out = RoomOut.model_validate(room)
    out.device_count = count
    return out


async def _device_count(ctx: Ctx, room_id: uuid.UUID) -> int:
    return (await ctx.db.scalar(select(func.count()).where(Device.room_id == room_id))) or 0


def _event(r: Room) -> dict:
    return {"id": str(r.id), "name": r.name}


@router.get("", response_model=list[RoomOut])
async def list_rooms(ctx: Ctx = Depends(require("core.rooms.read", any_room=True))):
    counts = (
        select(Device.room_id, func.count().label("n"))
        .where(Device.org_id == ctx.org_id)
        .group_by(Device.room_id)
        .subquery()
    )
    stmt = ctx.select(Room, func.coalesce(counts.c.n, 0)).outerjoin(counts, counts.c.room_id == Room.id).order_by(Room.name)
    allowed = ctx.principal.rooms_with("core.rooms.read")
    if allowed is not None:
        stmt = stmt.where(Room.id.in_(allowed))
    return [_out(r, n) for r, n in (await ctx.db.execute(stmt)).all()]


@router.post("", response_model=RoomOut, status_code=201)
async def create_room(body: RoomIn, ctx: Ctx = Depends(require("core.rooms.write"))):
    room = Room(org_id=ctx.org_id, **body.model_dump())
    ctx.db.add(room)
    await ctx.db.flush()
    ctx.audit("core.room.create", room, body.model_dump())
    ctx.emit("core.room.created", _event(room))
    await ctx.commit()
    return _out(room)


@router.get("/{room_id}", response_model=RoomOut)
async def get_room(room_id: uuid.UUID, ctx: Ctx = Depends(require("core.rooms.read", any_room=True))):
    room = await ctx.get(Room, room_id)
    ctx.check("core.rooms.read", room.id)
    return _out(room, await _device_count(ctx, room.id))


@router.patch("/{room_id}", response_model=RoomOut)
async def update_room(room_id: uuid.UUID, body: RoomUpdate, ctx: Ctx = Depends(require("core.rooms.write"))):
    room = await ctx.get(Room, room_id)
    changes = body.model_dump(exclude_unset=True)
    if "subnets" in changes and changes["subnets"] is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "subnets cannot be null")
    subnets_changed = "subnets" in changes and changes["subnets"] != room.subnets
    for k, v in changes.items():
        setattr(room, k, v)
    ctx.audit("core.room.update", room, changes)
    ctx.emit("core.room.updated", _event(room))
    if subnets_changed:  # IP -> room mapping changed
        emit_policy_changed(ctx, room_id=room.id)
    await ctx.commit()
    return _out(room, await _device_count(ctx, room.id))


@router.delete("/{room_id}", status_code=204)
async def delete_room(room_id: uuid.UUID, ctx: Ctx = Depends(require("core.rooms.write"))):
    room = await ctx.get(Room, room_id)
    removed = await remove_assignments_for(ctx, TargetType.ROOM, room.id)
    ctx.audit("core.room.delete", room, {"name": room.name, "removed_assignments": removed})
    ctx.emit("core.room.deleted", _event(room))
    emit_policy_changed(ctx, room_id=room.id)  # its devices and subnets lose the room's policies
    await ctx.db.delete(room)
    await ctx.commit()
