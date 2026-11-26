# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Buildings: the scope a manager administers. Rooms sit in one building (or in none).

Reading is scoped: a building manager (or teacher) sees only their own buildings, and
another building answers 404 as if it didn't exist. Creating, renaming and deleting
needs the org-wide core.buildings.write (unit managers and admins)."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from smp.core.api import ORMModel
from smp.core.context import Ctx, require
from smp.core.errors import AppError
from smp.core.models import Building, Policy, Room, Schedule, TargetType
from smp.core.policy import emit_policy_changed, remove_assignments_for

router = APIRouter(prefix="/buildings", tags=["buildings"])


class BuildingIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class BuildingUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None


class BuildingOut(ORMModel):
    id: uuid.UUID
    name: str
    description: str
    room_count: int = 0


def _out(b: Building, rooms: int = 0) -> BuildingOut:
    return BuildingOut(id=b.id, name=b.name, description=b.description, room_count=rooms)


def _event(b: Building) -> dict:
    return {"id": str(b.id), "name": b.name}


async def _room_count(ctx: Ctx, building_id: uuid.UUID) -> int:
    return (await ctx.db.scalar(select(func.count()).where(Room.building_id == building_id))) or 0


async def _check_name_free(ctx: Ctx, name: str, own_id: uuid.UUID | None = None) -> None:
    stmt = select(Building.id).where(Building.org_id == ctx.org_id, Building.name == name)
    if own_id is not None:
        stmt = stmt.where(Building.id != own_id)
    if await ctx.db.scalar(stmt) is not None:
        raise AppError(409, "buildings.name_taken", f"a building named {name!r} already exists", name=name)


async def _visible(ctx: Ctx, building_id: uuid.UUID) -> Building:
    """A building the caller may read; 404 (not 403) for one outside their scope, so
    its existence isn't revealed."""
    building = await ctx.get(Building, building_id)
    if not ctx.principal.can("core.buildings.read", building_id=building.id):  # type: ignore[union-attr]
        raise AppError(404, "not_found", "Building not found", entity="Building")
    return building


@router.get("", response_model=list[BuildingOut])
async def list_buildings(ctx: Ctx = Depends(require("core.buildings.read", any_room=True))):
    counts = (
        select(Room.building_id, func.count().label("n"))
        .where(Room.org_id == ctx.org_id, Room.building_id.is_not(None))
        .group_by(Room.building_id)
        .subquery()
    )
    stmt = (
        ctx.select(Building, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.building_id == Building.id)
        .order_by(Building.name)
    )
    allowed = ctx.principal.buildings_with("core.buildings.read")  # type: ignore[union-attr]
    if allowed is not None:
        stmt = stmt.where(Building.id.in_(allowed))
    return [_out(b, n) for b, n in (await ctx.db.execute(stmt)).all()]


@router.post("", response_model=BuildingOut, status_code=201)
async def create_building(body: BuildingIn, ctx: Ctx = Depends(require("core.buildings.write"))):
    await _check_name_free(ctx, body.name)
    building = Building(org_id=ctx.org_id, name=body.name, description=body.description)
    ctx.db.add(building)
    await ctx.db.flush()
    ctx.audit("core.building.create", building, body.model_dump())
    ctx.emit("core.building.created", _event(building))
    await ctx.commit()
    return _out(building)


@router.get("/{building_id}", response_model=BuildingOut)
async def get_building(building_id: uuid.UUID, ctx: Ctx = Depends(require("core.buildings.read", any_room=True))):
    building = await _visible(ctx, building_id)
    return _out(building, await _room_count(ctx, building.id))


@router.patch("/{building_id}", response_model=BuildingOut)
async def update_building(
    building_id: uuid.UUID, body: BuildingUpdate, ctx: Ctx = Depends(require("core.buildings.write"))
):
    building = await ctx.get(Building, building_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("name") is None:
        changes.pop("name", None)
    if changes.get("description") is None:
        changes.pop("description", None)
    if "name" in changes:
        await _check_name_free(ctx, changes["name"], own_id=building.id)
    for k, v in changes.items():
        setattr(building, k, v)
    ctx.audit("core.building.update", building, changes)
    ctx.emit("core.building.updated", _event(building))
    await ctx.commit()
    return _out(building, await _room_count(ctx, building.id))


@router.delete("/{building_id}", status_code=204)
async def delete_building(building_id: uuid.UUID, ctx: Ctx = Depends(require("core.buildings.write"))):
    building = await ctx.get(Building, building_id)
    rooms = await _room_count(ctx, building.id)
    policies = (await ctx.db.scalar(select(func.count()).where(Policy.building_id == building.id))) or 0
    schedules = (await ctx.db.scalar(select(func.count()).where(Schedule.building_id == building.id))) or 0
    if rooms or policies or schedules:
        raise AppError(
            409,
            "buildings.not_empty",
            "the building still has rooms, policies or schedules: move or delete them first",
            rooms=rooms,
            policies=policies,
            schedules=schedules,
        )
    removed = await remove_assignments_for(ctx, TargetType.BUILDING, building.id)
    ctx.audit("core.building.delete", building, {"name": building.name, "removed_assignments": removed})
    ctx.emit("core.building.deleted", _event(building))
    emit_policy_changed(ctx, building_id=building.id)  # its building-level policy assignments are gone
    await ctx.db.delete(building)
    await ctx.commit()
