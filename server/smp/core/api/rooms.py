# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

import ipaddress
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from smp.core.api import ORMModel
from smp.core.context import Ctx, require
from smp.core.errors import AppError
from smp.core.models import Building, Device, Room, TargetType
from smp.core.policy import emit_policy_changed, remove_assignments_for

router = APIRouter(prefix="/rooms", tags=["rooms"])


def _normalise_subnets(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    return [str(ipaddress.ip_network(s, strict=False)) for s in v]


class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    building_id: uuid.UUID | None = None
    description: str = ""
    subnets: list[str] = []

    v_subnets = field_validator("subnets")(_normalise_subnets)


class RoomUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    building_id: uuid.UUID | None = None  # null moves the room out of its building
    description: str | None = None
    subnets: list[str] | None = None

    v_subnets = field_validator("subnets")(_normalise_subnets)


class RoomOut(ORMModel):
    id: uuid.UUID
    name: str
    building_id: uuid.UUID | None
    building: str  # the building's name, "" if none (read-only: set building_id)
    description: str
    subnets: list[str]
    device_count: int = 0


def _out(room: Room, count: int = 0) -> RoomOut:
    b = room.building
    return RoomOut(
        id=room.id,
        name=room.name,
        building_id=b.id if b else None,
        building=b.name if b else "",
        description=room.description,
        subnets=room.subnets,
        device_count=count,
    )


async def _building(ctx: Ctx, building_id: uuid.UUID | None) -> Building | None:
    """The building (of this org) a request names, or 404; None for no building."""
    return await ctx.get(Building, building_id) if building_id is not None else None


def _check_subnets(ctx: Ctx) -> None:
    """Subnets map IP addresses to rooms school-wide (devices whose room follows the
    network, DNS, agents without a room): a broad subnet on one room would pull other
    buildings' machines into it. So only an org-wide grant (unit managers, admins)
    may set them; building managers manage their rooms without them."""
    if not ctx.principal.can("core.rooms.write"):  # type: ignore[union-attr]
        raise AppError(
            status.HTTP_403_FORBIDDEN, "rooms.subnets_unit_only", "only unit-level roles may set a room's subnets"
        )


def _check_write_in(ctx: Ctx, building_id: uuid.UUID | None) -> None:
    """The caller may write rooms in this building (None: rooms in no building, which
    only an org-wide grant reaches). Checked before the building is looked up, so a
    manager can't tell another building's id from a missing one."""
    if building_id is None:
        ctx.check("core.rooms.write")
    else:
        ctx.check("core.rooms.write", building_id=building_id)


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
async def create_room(body: RoomIn, ctx: Ctx = Depends(require("core.rooms.write", any_room=True))):
    _check_write_in(ctx, body.building_id)
    if body.subnets:
        _check_subnets(ctx)
    building = await _building(ctx, body.building_id)
    room = Room(org_id=ctx.org_id, building=building, **body.model_dump(exclude={"building_id"}))
    ctx.db.add(room)
    await ctx.db.flush()
    ctx.audit("core.room.create", room, body.model_dump(mode="json"))
    ctx.emit("core.room.created", _event(room))
    await ctx.commit()
    return _out(room)


@router.get("/{room_id}", response_model=RoomOut)
async def get_room(room_id: uuid.UUID, ctx: Ctx = Depends(require("core.rooms.read", any_room=True))):
    room = await ctx.get(Room, room_id)
    ctx.check("core.rooms.read", room.id)
    return _out(room, await _device_count(ctx, room.id))


@router.patch("/{room_id}", response_model=RoomOut)
async def update_room(
    room_id: uuid.UUID, body: RoomUpdate, ctx: Ctx = Depends(require("core.rooms.write", any_room=True))
):
    room = await ctx.get(Room, room_id)
    ctx.check("core.rooms.write", room_id=room.id)  # the room's current building
    changes = body.model_dump(exclude_unset=True)
    if "subnets" in changes and changes["subnets"] is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "subnets cannot be null")
    subnets_changed = "subnets" in changes and changes["subnets"] != room.subnets
    if subnets_changed:
        _check_subnets(ctx)
    audit_buildings = None  # None: the room's building, as ctx.audit finds it
    moved = False
    if "building_id" in changes:
        old_id = room.building_id
        if changes["building_id"] != old_id:
            _check_write_in(ctx, changes["building_id"])  # the destination, too
        new = await _building(ctx, changes.pop("building_id"))
        moved = (new.id if new else None) != old_id
        room.building, room.building_id = new, new.id if new else None
        changes["building_id"] = str(new.id) if new else None  # JSON for the audit entry
        audit_buildings = [i for i in (old_id, room.building_id) if i is not None] or None
    for k, v in changes.items():
        if k != "building_id":
            setattr(room, k, v)
    ctx.audit("core.room.update", room, changes, building_ids=audit_buildings)
    ctx.emit("core.room.updated", _event(room))
    if subnets_changed or moved:  # IP -> room mapping changed, or the room's building-level policies
        emit_policy_changed(ctx, room_id=room.id)
    await ctx.commit()
    return _out(room, await _device_count(ctx, room.id))


@router.delete("/{room_id}", status_code=204)
async def delete_room(room_id: uuid.UUID, ctx: Ctx = Depends(require("core.rooms.write", any_room=True))):
    room = await ctx.get(Room, room_id)
    ctx.check("core.rooms.write", room_id=room.id)
    removed = await remove_assignments_for(ctx, TargetType.ROOM, room.id)
    ctx.audit("core.room.delete", room, {"name": room.name, "removed_assignments": removed})
    ctx.emit("core.room.deleted", _event(room))
    emit_policy_changed(ctx, room_id=room.id)  # its devices and subnets lose the room's policies
    await ctx.db.delete(room)
    await ctx.commit()
