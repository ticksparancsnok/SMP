# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, IPvAnyAddress, ValidationError, field_validator, model_validator
from sqlalchemy import case, func, select

from smp.core.api import ORMModel
from smp.core.context import Ctx, require
from smp.core.errors import AppError
from smp.core.models import Device, Group, Policy, PolicyAssignment, Room, Schedule, TargetType, User
from smp.core.policy import effective_policies, emit_policy_changed, resolve_ip, validate_windows

router = APIRouter(tags=["policies"])


# --------------------------------------------------------------------------- schemas


class PolicyIn(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    data: dict[str, Any] = {}


class PolicyUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    data: dict[str, Any] | None = None


class PolicyOut(ORMModel):
    id: uuid.UUID
    kind: str
    name: str
    description: str
    data: dict[str, Any]
    updated_at: datetime


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    timezone: str = "Europe/Budapest"
    windows: list[dict[str, Any]] = []

    @field_validator("windows")
    @classmethod
    def _windows(cls, v):
        validate_windows(v)
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        from zoneinfo import ZoneInfo

        ZoneInfo(v)  # raises for unknown zones
        return v


class ScheduleOut(ORMModel):
    id: uuid.UUID
    name: str
    timezone: str
    windows: list[dict[str, Any]]


class AssignmentIn(BaseModel):
    policy_id: uuid.UUID
    target_type: TargetType
    target_id: uuid.UUID | None = None
    schedule_id: uuid.UUID | None = None
    priority: int = Field(0, ge=-1000, le=1000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    enabled: bool = True
    label: str = ""

    @model_validator(mode="after")
    def _target(self):
        if (self.target_type == TargetType.ORG) != (self.target_id is None):
            raise ValueError("target_id is required unless target_type is 'org' (and must be empty then)")
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class AssignmentOut(ORMModel):
    id: uuid.UUID
    policy_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID | None
    schedule_id: uuid.UUID | None
    priority: int
    starts_at: datetime | None
    ends_at: datetime | None
    enabled: bool
    label: str
    created_by: uuid.UUID | None
    # Display name of the target (room/group name, hostname, "Name (username)"); None
    # for the whole school or when the target no longer exists. Filled by the list.
    target_name: str | None = None


# --------------------------------------------------------------------------- policies


def _validate_policy_data(ctx: Ctx, kind: str, data: dict[str, Any]) -> dict[str, Any]:
    model = ctx.registry.policy_model(kind)
    if model is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"no enabled module defines policies of kind {kind!r}")
    try:
        return model.model_validate(data).model_dump(mode="json")
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.errors(include_url=False)) from e


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(kind: str | None = None, ctx: Ctx = Depends(require("core.policies.read", any_room=True))):
    stmt = ctx.select(Policy).order_by(Policy.kind, Policy.name)
    if kind:
        stmt = stmt.where(Policy.kind == kind)
    return list((await ctx.db.execute(stmt)).scalars())


@router.post("/policies", response_model=PolicyOut, status_code=201)
async def create_policy(body: PolicyIn, ctx: Ctx = Depends(require("core.policies.write"))):
    data = _validate_policy_data(ctx, body.kind, body.data)
    p = Policy(org_id=ctx.org_id, kind=body.kind, name=body.name, description=body.description, data=data)
    ctx.db.add(p)
    await ctx.db.flush()
    ctx.audit("core.policy.create", p, {"kind": p.kind, "name": p.name, "data": data})
    await ctx.commit()
    return p


@router.get("/policies/{policy_id}", response_model=PolicyOut)
async def get_policy(policy_id: uuid.UUID, ctx: Ctx = Depends(require("core.policies.read", any_room=True))):
    return await ctx.get(Policy, policy_id)


@router.patch("/policies/{policy_id}", response_model=PolicyOut)
async def update_policy(policy_id: uuid.UUID, body: PolicyUpdate, ctx: Ctx = Depends(require("core.policies.write"))):
    p = await ctx.get(Policy, policy_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("data") is not None:
        changes["data"] = _validate_policy_data(ctx, p.kind, changes["data"])
    for k, v in changes.items():
        if v is not None:
            setattr(p, k, v)
    ctx.audit("core.policy.update", p, changes)
    emit_policy_changed(ctx, policy_id=p.id, kind=p.kind)
    await ctx.commit()
    return p


@router.delete("/policies/{policy_id}", status_code=204)
async def delete_policy(policy_id: uuid.UUID, ctx: Ctx = Depends(require("core.policies.write"))):
    p = await ctx.get(Policy, policy_id)
    ctx.audit("core.policy.delete", p, {"kind": p.kind, "name": p.name})
    emit_policy_changed(ctx, policy_id=p.id, kind=p.kind)
    await ctx.db.delete(p)
    await ctx.commit()


# --------------------------------------------------------------------------- schedules


@router.get("/schedules", response_model=list[ScheduleOut])
async def list_schedules(ctx: Ctx = Depends(require("core.policies.read", any_room=True))):
    return list((await ctx.db.execute(ctx.select(Schedule).order_by(Schedule.name))).scalars())


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
async def create_schedule(body: ScheduleIn, ctx: Ctx = Depends(require("core.policies.write"))):
    s = Schedule(org_id=ctx.org_id, **body.model_dump())
    ctx.db.add(s)
    await ctx.db.flush()
    ctx.audit("core.schedule.create", s, body.model_dump())
    await ctx.commit()
    return s


@router.put("/schedules/{schedule_id}", response_model=ScheduleOut)
async def replace_schedule(schedule_id: uuid.UUID, body: ScheduleIn, ctx: Ctx = Depends(require("core.policies.write"))):
    s = await ctx.get(Schedule, schedule_id)
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    ctx.audit("core.schedule.update", s, body.model_dump())
    emit_policy_changed(ctx, schedule_id=s.id)
    await ctx.commit()
    return s


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: uuid.UUID, ctx: Ctx = Depends(require("core.policies.write"))):
    s = await ctx.get(Schedule, schedule_id)
    # The FK is ON DELETE SET NULL, which would silently turn "only during lessons"
    # into "always". Make the admin remove or change those assignments first.
    in_use = await ctx.db.scalar(
        select(func.count()).where(PolicyAssignment.org_id == ctx.org_id, PolicyAssignment.schedule_id == s.id)
    )
    if in_use:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "policies.schedule_in_use",
            f"schedule is used by {in_use} policy assignment(s); remove those first",
            count=in_use,
        )
    ctx.audit("core.schedule.delete", s, {"name": s.name})
    emit_policy_changed(ctx, schedule_id=s.id)
    await ctx.db.delete(s)
    await ctx.commit()


# --------------------------------------------------------------------------- assignments

_TARGET_MODELS = {TargetType.ROOM: Room, TargetType.GROUP: Group, TargetType.DEVICE: Device, TargetType.USER: User}


async def _check_assign(
    ctx: Ctx, target_type: TargetType, target_id: uuid.UUID | None, *, allow_missing: bool = False
) -> None:
    """Teachers with a room-scoped grant may assign policies to their rooms and the
    devices in them (e.g. start exam mode); everything else needs an org-wide grant.

    allow_missing: when removing an assignment whose target no longer exists, don't
    404. room_id stays None, so only an org-wide grant may remove it."""
    room_id: uuid.UUID | None = None
    if target_type != TargetType.ORG and target_id is not None:
        model = _TARGET_MODELS[target_type]
        obj = await ctx.db.get(model, target_id)
        if obj is None or obj.org_id != ctx.org_id:
            if not allow_missing:
                raise AppError(status.HTTP_404_NOT_FOUND, "not_found", f"{model.__name__} not found", entity=model.__name__)
        elif target_type == TargetType.ROOM:
            room_id = obj.id
        elif target_type == TargetType.DEVICE:
            room_id = obj.room_id
    ctx.check("core.policies.assign", room_id)


@router.get("/policy-assignments", response_model=list[AssignmentOut])
async def list_assignments(
    ctx: Ctx = Depends(require("core.policies.read", any_room=True)),
    policy_id: uuid.UUID | None = None,
    target_type: TargetType | None = None,
    target_id: uuid.UUID | None = None,
):
    stmt = ctx.select(PolicyAssignment).order_by(PolicyAssignment.created_at)
    if policy_id:
        stmt = stmt.where(PolicyAssignment.policy_id == policy_id)
    if target_type:
        stmt = stmt.where(PolicyAssignment.target_type == target_type)
    if target_id:
        stmt = stmt.where(PolicyAssignment.target_id == target_id)
    rows = list((await ctx.db.execute(stmt)).scalars())
    names = await _target_names(ctx, rows)
    return [
        AssignmentOut.model_validate(a).model_copy(update={"target_name": names.get((a.target_type, a.target_id))})
        for a in rows
    ]


async def _target_names(ctx: Ctx, rows: list[PolicyAssignment]) -> dict[tuple[str, uuid.UUID], str]:
    """Names of the assignments' targets, one query per target type, so the UI can show
    them without downloading every device and user of the school. Only names the caller
    could read from the matching list endpoint (/rooms, /devices, /groups, /users)."""
    wanted: dict[TargetType, set[uuid.UUID]] = {}
    for a in rows:
        if a.target_id is not None and a.target_type != TargetType.ORG:
            wanted.setdefault(TargetType(a.target_type), set()).add(a.target_id)
    p = ctx.principal
    names: dict[tuple[str, uuid.UUID], str] = {}
    for tt, ids in wanted.items():
        if tt == TargetType.ROOM:
            if not p.can_anywhere("core.rooms.read"):
                continue
            stmt = select(Room.id, Room.name).where(Room.org_id == ctx.org_id, Room.id.in_(ids))
            if (allowed := p.rooms_with("core.rooms.read")) is not None:
                stmt = stmt.where(Room.id.in_(allowed))
        elif tt == TargetType.DEVICE:
            if not p.can_anywhere("core.devices.read"):
                continue
            stmt = select(Device.id, Device.hostname).where(Device.org_id == ctx.org_id, Device.id.in_(ids))
            if (allowed := p.rooms_with("core.devices.read")) is not None:
                stmt = stmt.where(Device.room_id.in_(allowed))
        elif tt == TargetType.GROUP:
            if not p.can("core.groups.read"):
                continue
            stmt = select(Group.id, Group.name).where(Group.org_id == ctx.org_id, Group.id.in_(ids))
        else:  # USER
            if not p.can("core.users.read"):
                continue
            stmt = select(
                User.id,
                case(
                    (User.display_name.in_(["", User.username]), User.username),
                    else_=User.display_name + " (" + User.username + ")",
                ),
            ).where(User.org_id == ctx.org_id, User.id.in_(ids))
        for tid, name in await ctx.db.execute(stmt):
            names[(tt.value, tid)] = name
    return names


@router.post("/policy-assignments", response_model=AssignmentOut, status_code=201)
async def create_assignment(body: AssignmentIn, ctx: Ctx = Depends(require("core.policies.assign", any_room=True))):
    await _check_assign(ctx, body.target_type, body.target_id)
    policy = await ctx.get(Policy, body.policy_id)
    if body.schedule_id:
        await ctx.get(Schedule, body.schedule_id)
    a = PolicyAssignment(org_id=ctx.org_id, created_by=ctx.principal.id, **body.model_dump())
    ctx.db.add(a)
    await ctx.db.flush()
    ctx.audit("core.policy.assign", a, {**body.model_dump(mode="json"), "policy_name": policy.name, "kind": policy.kind})
    emit_policy_changed(ctx, assignment_id=a.id, kind=policy.kind, target_type=a.target_type, target_id=a.target_id)
    await ctx.commit()
    return a


@router.delete("/policy-assignments/{assignment_id}", status_code=204)
async def delete_assignment(assignment_id: uuid.UUID, ctx: Ctx = Depends(require("core.policies.assign", any_room=True))):
    a = await ctx.get(PolicyAssignment, assignment_id)
    await _check_assign(ctx, TargetType(a.target_type), a.target_id, allow_missing=True)
    ctx.audit("core.policy.unassign", a, {"policy_id": str(a.policy_id), "target_type": a.target_type})
    emit_policy_changed(ctx, assignment_id=a.id, kind=a.policy.kind, target_type=a.target_type, target_id=a.target_id)
    await ctx.db.delete(a)
    await ctx.commit()


# --------------------------------------------------------------------------- resolution


@router.get("/policies-resolve")
async def resolve(
    ctx: Ctx = Depends(require("core.policies.read")),
    ip: IPvAnyAddress | None = None,
    device_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    kind: list[str] | None = Query(None),
    at: datetime | None = None,
) -> dict[str, Any]:
    """Debug/inspection endpoint: what applies to this IP / device / user at `at` (default now)."""
    device = room = None
    if device_id:
        device = await ctx.get(Device, device_id)
    elif ip:
        device, room = await resolve_ip(ctx.db, ctx.org_id, str(ip))
    room_id = room.id if room else (device.room_id if device else None)
    result = await effective_policies(
        ctx.db, ctx.org_id, device=device, room_id=room_id, user_id=user_id, kinds=set(kind) if kind else None, at=at
    )
    return {
        "device_id": str(device.id) if device else None,
        "room_id": str(room_id) if room_id else None,
        "policies": {k: ep.as_dict() for k, ep in result.items()},
    }
