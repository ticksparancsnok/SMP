# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, IPvAnyAddress, ValidationError, field_validator, model_validator
from sqlalchemy import ColumnElement, case, func, or_, select

from smp.core.api import ORMModel
from smp.core.context import Ctx, get_ctx, require
from smp.core.errors import AppError
from smp.core.models import (
    Building,
    Device,
    Group,
    GroupBuilding,
    Policy,
    PolicyAssignment,
    RoleAssignment,
    Room,
    Schedule,
    TargetType,
    User,
)
from smp.core.policy import (
    assignment_scope,
    effective_policies,
    emit_policy_changed,
    group_within,
    resolve_ip,
    user_within,
    validate_windows,
)

router = APIRouter(tags=["policies"])


# --------------------------------------------------------------------------- schemas


class PolicyIn(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    data: dict[str, Any] = {}
    building_id: uuid.UUID | None = None  # owner; None = school-wide (unit level edits it)


class PolicyUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    data: dict[str, Any] | None = None
    building_id: uuid.UUID | None = None  # owner; send null to make it school-wide


class PolicyOut(ORMModel):
    id: uuid.UUID
    building_id: uuid.UUID | None
    kind: str
    name: str
    description: str
    data: dict[str, Any]
    updated_at: datetime


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    timezone: str = "Europe/Budapest"
    windows: list[dict[str, Any]] = []
    building_id: uuid.UUID | None = None  # owner; None = school-wide (unit level edits it)

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
    building_id: uuid.UUID | None
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


def _deny(perm: str) -> AppError:
    return AppError(status.HTTP_403_FORBIDDEN, "permission.missing", f"missing permission {perm}", permission=perm)


async def _check_owner(ctx: Ctx, building_id: uuid.UUID | None) -> None:
    """May the caller write policies/schedules owned by this building (None = school-wide,
    which needs the org-wide grant)? A building that doesn't exist is a 404."""
    ctx.check("core.policies.write", building_id=building_id)
    if building_id is not None:
        await ctx.get(Building, building_id)


def _readable_buildings(ctx: Ctx) -> set[uuid.UUID] | None:
    """Buildings whose own policies/schedules the caller may read; None = all."""
    assert ctx.principal is not None
    return ctx.principal.buildings_with("core.policies.read")


def _readable_owner(ctx: Ctx, model: type[Policy] | type[Schedule]) -> ColumnElement[bool] | None:
    """Policies/schedules the caller may list: all with an org-wide read, else the
    school-wide ones (a manager assigns those) and those owned by their buildings."""
    buildings = _readable_buildings(ctx)
    if buildings is None:
        return None
    return or_(model.building_id.is_(None), model.building_id.in_(list(buildings)))


def _may_use(ctx: Ctx, owner_id: uuid.UUID | None) -> bool:
    """May the caller put a policy/schedule with this owner into an assignment? Anyone
    with an org-wide assign grant may; the rest only school-wide ones and their own
    buildings' (a room teacher only school-wide ones)."""
    p = ctx.principal
    assert p is not None
    return p.can("core.policies.assign") or owner_id is None or owner_id in p.scope_building_ids


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(kind: str | None = None, ctx: Ctx = Depends(require("core.policies.read", any_room=True))):
    stmt = ctx.select(Policy).order_by(Policy.kind, Policy.name)
    if (visible := _readable_owner(ctx, Policy)) is not None:
        stmt = stmt.where(visible)
    if kind:
        stmt = stmt.where(Policy.kind == kind)
    return list((await ctx.db.execute(stmt)).scalars())


@router.post("/policies", response_model=PolicyOut, status_code=201)
async def create_policy(body: PolicyIn, ctx: Ctx = Depends(require("core.policies.write", any_room=True))):
    await _check_owner(ctx, body.building_id)
    data = _validate_policy_data(ctx, body.kind, body.data)
    p = Policy(
        org_id=ctx.org_id,
        building_id=body.building_id,
        kind=body.kind,
        name=body.name,
        description=body.description,
        data=data,
    )
    ctx.db.add(p)
    await ctx.db.flush()
    ctx.audit(
        "core.policy.create",
        p,
        {"kind": p.kind, "name": p.name, "data": data, "building_id": _str_or_none(p.building_id)},
    )
    await ctx.commit()
    return p


@router.get("/policies/{policy_id}", response_model=PolicyOut)
async def get_policy(policy_id: uuid.UUID, ctx: Ctx = Depends(require("core.policies.read", any_room=True))):
    p = await ctx.get(Policy, policy_id)
    buildings = _readable_buildings(ctx)
    if buildings is not None and p.building_id is not None and p.building_id not in buildings:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Policy not found", entity="Policy")
    return p


@router.patch("/policies/{policy_id}", response_model=PolicyOut)
async def update_policy(
    policy_id: uuid.UUID, body: PolicyUpdate, ctx: Ctx = Depends(require("core.policies.write", any_room=True))
):
    p = await ctx.get(Policy, policy_id)
    changes = body.model_dump(exclude_unset=True)
    old_owner = p.building_id
    await _check_owner(ctx, old_owner)
    if "building_id" in changes and changes["building_id"] != old_owner:
        await _check_owner(ctx, changes["building_id"])  # moving it needs both ends
    if changes.get("data") is not None:
        changes["data"] = _validate_policy_data(ctx, p.kind, changes["data"])
    for k, v in changes.items():
        if v is not None or k == "building_id":
            setattr(p, k, v)
    owners = [b for b in {old_owner, p.building_id} if b is not None]
    ctx.audit(
        "core.policy.update",
        p,
        {k: (_str_or_none(v) if k == "building_id" else v) for k, v in changes.items()},
        building_ids=owners or None,
    )
    emit_policy_changed(ctx, policy_id=p.id, kind=p.kind)
    await ctx.commit()
    return p


@router.delete("/policies/{policy_id}", status_code=204)
async def delete_policy(policy_id: uuid.UUID, ctx: Ctx = Depends(require("core.policies.write", any_room=True))):
    p = await ctx.get(Policy, policy_id)
    await _check_owner(ctx, p.building_id)
    ctx.audit("core.policy.delete", p, {"kind": p.kind, "name": p.name})
    emit_policy_changed(ctx, policy_id=p.id, kind=p.kind)
    await ctx.db.delete(p)
    await ctx.commit()


def _str_or_none(v: Any) -> str | None:
    return str(v) if v is not None else None


# --------------------------------------------------------------------------- schedules


@router.get("/schedules", response_model=list[ScheduleOut])
async def list_schedules(ctx: Ctx = Depends(require("core.policies.read", any_room=True))):
    stmt = ctx.select(Schedule).order_by(Schedule.name)
    if (visible := _readable_owner(ctx, Schedule)) is not None:
        stmt = stmt.where(visible)
    return list((await ctx.db.execute(stmt)).scalars())


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
async def create_schedule(body: ScheduleIn, ctx: Ctx = Depends(require("core.policies.write", any_room=True))):
    await _check_owner(ctx, body.building_id)
    s = Schedule(org_id=ctx.org_id, **body.model_dump())
    ctx.db.add(s)
    await ctx.db.flush()
    ctx.audit("core.schedule.create", s, body.model_dump(mode="json"))
    await ctx.commit()
    return s


@router.put("/schedules/{schedule_id}", response_model=ScheduleOut)
async def replace_schedule(
    schedule_id: uuid.UUID, body: ScheduleIn, ctx: Ctx = Depends(require("core.policies.write", any_room=True))
):
    s = await ctx.get(Schedule, schedule_id)
    old_owner = s.building_id
    # A body without building_id keeps the current owner (an old client must not turn a
    # building's schedule into a school-wide one by accident); an explicit null does.
    values = body.model_dump(exclude={"building_id"} if "building_id" not in body.model_fields_set else set())
    new_owner = values.get("building_id", old_owner)
    await _check_owner(ctx, old_owner)
    if new_owner != old_owner:
        await _check_owner(ctx, new_owner)
    for k, v in values.items():
        setattr(s, k, v)
    owners = [b for b in {old_owner, new_owner} if b is not None]
    ctx.audit("core.schedule.update", s, body.model_dump(mode="json"), building_ids=owners or None)
    emit_policy_changed(ctx, schedule_id=s.id)
    await ctx.commit()
    return s


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: uuid.UUID, ctx: Ctx = Depends(require("core.policies.write", any_room=True))
):
    s = await ctx.get(Schedule, schedule_id)
    await _check_owner(ctx, s.building_id)
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

_TARGET_MODELS = {
    TargetType.BUILDING: Building,
    TargetType.ROOM: Room,
    TargetType.GROUP: Group,
    TargetType.DEVICE: Device,
    TargetType.USER: User,
}


async def _assigner(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    """Gate for the assignment endpoints: some assign grant (rooms) or a policy-write
    grant (buildings). The target decides in _check_assign."""
    p = ctx.principal
    assert p is not None
    if not (p.can_anywhere("core.policies.assign") or p.can_anywhere("core.policies.write")):
        raise _deny("core.policies.assign")
    return ctx


async def _check_assign(
    ctx: Ctx, target_type: TargetType, target_id: uuid.UUID | None, *, allow_missing: bool = False
) -> Any:
    """May the caller assign policies to / remove them from this target? Returns the
    target object (None for the org or a vanished target).

      org            only an org-wide assign grant
      room, device   assign for that room (teachers: their rooms; managers: the rooms of
                     their buildings); a device in no room: org-wide grant only
      building       org-wide assign, or core.policies.write for that building
      group, user    org-wide assign, or core.policies.write where the group lies in
                     buildings the caller manages entirely (>= 1), or the user's roles
                     all lie in such buildings (>= 1)

    allow_missing: when removing an assignment whose target no longer exists, don't
    404; only an org-wide grant may remove it."""
    obj: Any = None
    if target_type != TargetType.ORG and target_id is not None:
        model = _TARGET_MODELS[target_type]
        obj = await ctx.db.get(model, target_id)
        if obj is None or obj.org_id != ctx.org_id:
            obj = None
            if not allow_missing:
                raise AppError(status.HTTP_404_NOT_FOUND, "not_found", f"{model.__name__} not found", entity=model.__name__)
    p = ctx.principal
    assert p is not None
    if p.can("core.policies.assign"):
        return obj
    if target_type == TargetType.ROOM:
        ctx.check("core.policies.assign", obj.id if obj else None)
    elif target_type == TargetType.DEVICE:
        ctx.check("core.policies.assign", obj.room_id if obj else None)
    elif target_type == TargetType.BUILDING and obj is not None:
        ctx.check("core.policies.write", building_id=obj.id)
    elif target_type in (TargetType.GROUP, TargetType.USER) and obj is not None:
        if not await _within_managed(ctx, target_type, obj.id):
            raise _deny("core.policies.write")
    else:
        raise _deny("core.policies.assign")
    return obj


async def _within_managed(ctx: Ctx, target_type: TargetType, target_id: uuid.UUID) -> bool:
    """Does this group/user lie entirely in buildings where the caller may write policies?"""
    assert ctx.principal is not None
    buildings = ctx.principal.buildings_with("core.policies.write")
    if buildings is None:  # an org-wide write grant
        return True
    within = group_within if target_type == TargetType.GROUP else user_within
    return bool(await ctx.db.scalar(select(within(target_id, buildings))))


def _check_usable(ctx: Ctx, policy: Policy, schedule: Schedule | None) -> None:
    """The caller may only assign what they could see: school-wide policies/schedules
    and those of their own buildings (see _may_use)."""
    if not _may_use(ctx, policy.building_id) or (schedule is not None and not _may_use(ctx, schedule.building_id)):
        raise _deny("core.policies.assign")


async def _audit_buildings(ctx: Ctx, target_type: TargetType, obj: Any) -> list[uuid.UUID] | None:
    """The buildings an assignment is about (so their managers see it in the audit
    log): the target's building(s); None (org-level entry) for the org or when unknown."""
    if obj is None:
        return None
    ids: set[uuid.UUID | None] = set()
    if target_type == TargetType.BUILDING:
        ids.add(obj.id)
    elif target_type == TargetType.ROOM:
        ids.add(obj.building_id)
    elif target_type == TargetType.DEVICE:
        if obj.room_id is not None:
            ids.add(await ctx.db.scalar(select(Room.building_id).where(Room.id == obj.room_id)))
    elif target_type == TargetType.GROUP:
        ids.update(await ctx.db.scalars(select(GroupBuilding.building_id).where(GroupBuilding.group_id == obj.id)))
    elif target_type == TargetType.USER:
        rows = await ctx.db.execute(
            select(RoleAssignment.building_id, Room.building_id)
            .outerjoin(Room, Room.id == RoleAssignment.room_id)
            .where(RoleAssignment.user_id == obj.id)
        )
        for direct, via_room in rows:
            ids.update((direct, via_room))
    found = sorted(i for i in ids if i is not None)
    return found or None


@router.get("/policy-assignments", response_model=list[AssignmentOut])
async def list_assignments(
    ctx: Ctx = Depends(require("core.policies.read", any_room=True)),
    policy_id: uuid.UUID | None = None,
    target_type: TargetType | None = None,
    target_id: uuid.UUID | None = None,
):
    stmt = ctx.select(PolicyAssignment).order_by(PolicyAssignment.created_at)
    assert ctx.principal is not None
    if (in_scope := assignment_scope(ctx.principal)) is not None:
        stmt = stmt.where(in_scope)
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
    assert p is not None
    names: dict[tuple[str, uuid.UUID], str] = {}
    for tt, ids in wanted.items():
        if tt == TargetType.BUILDING:
            if not p.can_anywhere("core.buildings.read"):
                continue
            stmt = select(Building.id, Building.name).where(Building.org_id == ctx.org_id, Building.id.in_(ids))
            if (allowed_b := p.buildings_with("core.buildings.read")) is not None:
                stmt = stmt.where(Building.id.in_(list(allowed_b)))
        elif tt == TargetType.ROOM:
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
            if not p.can_anywhere("core.groups.read"):
                continue
            stmt = select(Group.id, Group.name).where(Group.org_id == ctx.org_id, Group.id.in_(ids))
            if (allowed_b := p.buildings_with("core.groups.read")) is not None:
                stmt = stmt.where(group_within(Group.id, allowed_b))
        else:  # USER
            if not p.can_anywhere("core.users.read"):
                continue
            stmt = select(
                User.id,
                case(
                    (User.display_name.in_(["", User.username]), User.username),
                    else_=User.display_name + " (" + User.username + ")",
                ),
            ).where(User.org_id == ctx.org_id, User.id.in_(ids))
            if (allowed_b := p.buildings_with("core.users.read")) is not None:
                stmt = stmt.where(user_within(User.id, allowed_b))
        for tid, name in await ctx.db.execute(stmt):
            names[(tt.value, tid)] = name
    return names


@router.post("/policy-assignments", response_model=AssignmentOut, status_code=201)
async def create_assignment(body: AssignmentIn, ctx: Ctx = Depends(_assigner)):
    target = await _check_assign(ctx, body.target_type, body.target_id)
    policy = await ctx.get(Policy, body.policy_id)
    schedule = await ctx.get(Schedule, body.schedule_id) if body.schedule_id else None
    _check_usable(ctx, policy, schedule)
    assert ctx.principal is not None
    a = PolicyAssignment(org_id=ctx.org_id, created_by=ctx.principal.id, **body.model_dump())
    ctx.db.add(a)
    await ctx.db.flush()
    ctx.audit(
        "core.policy.assign",
        a,
        {**body.model_dump(mode="json"), "policy_name": policy.name, "kind": policy.kind},
        building_ids=await _audit_buildings(ctx, body.target_type, target),
    )
    emit_policy_changed(ctx, assignment_id=a.id, kind=policy.kind, target_type=a.target_type, target_id=a.target_id)
    await ctx.commit()
    return a


@router.delete("/policy-assignments/{assignment_id}", status_code=204)
async def delete_assignment(assignment_id: uuid.UUID, ctx: Ctx = Depends(_assigner)):
    a = await ctx.get(PolicyAssignment, assignment_id)
    target = await _check_assign(ctx, TargetType(a.target_type), a.target_id, allow_missing=True)
    schedule = await ctx.db.get(Schedule, a.schedule_id) if a.schedule_id else None
    _check_usable(ctx, a.policy, schedule)
    ctx.audit(
        "core.policy.unassign",
        a,
        {"policy_id": str(a.policy_id), "target_type": a.target_type},
        building_ids=await _audit_buildings(ctx, TargetType(a.target_type), target),
    )
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
