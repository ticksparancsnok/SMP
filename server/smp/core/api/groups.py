# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from smp.core.api import ORMModel
from smp.core.context import Ctx, require
from smp.core.models import Group, GroupMember, GroupTeacher, RoleAssignment, TargetType, User
from smp.core.policy import emit_policy_changed, remove_assignments_for

router = APIRouter(prefix="/groups", tags=["groups"])


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field("custom", pattern=r"^(class|staff|custom)$")
    description: str = ""


class GroupUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    kind: str | None = Field(None, pattern=r"^(class|staff|custom)$")
    description: str | None = None


class GroupOut(ORMModel):
    id: uuid.UUID
    name: str
    kind: str
    description: str
    external_id: str | None
    member_count: int = 0
    teachers: list["MemberOut"] = []  # class teachers (may reset their students' passwords)


class MemberOut(ORMModel):
    id: uuid.UUID
    username: str
    display_name: str


GroupOut.model_rebuild()


def _out(g: Group, count: int) -> GroupOut:
    out = GroupOut.model_validate(g)
    out.member_count = count
    return out


@router.get("", response_model=list[GroupOut])
async def list_groups(ctx: Ctx = Depends(require("core.groups.read"))):
    counts = (
        select(GroupMember.group_id, func.count().label("n"))
        .where(GroupMember.org_id == ctx.org_id)
        .group_by(GroupMember.group_id)
        .subquery()
    )
    rows = await ctx.db.execute(
        ctx.select(Group, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.group_id == Group.id)
        .order_by(Group.name)
    )
    return [_out(g, n) for g, n in rows.all()]


@router.post("", response_model=GroupOut, status_code=201)
async def create_group(body: GroupIn, ctx: Ctx = Depends(require("core.groups.write"))):
    g = Group(org_id=ctx.org_id, **body.model_dump())
    ctx.db.add(g)
    await ctx.db.flush()
    ctx.audit("core.group.create", g, body.model_dump())
    ctx.emit("core.group.created", {"id": str(g.id), "name": g.name})
    await ctx.commit()
    await ctx.db.refresh(g, ["teachers"])  # avoid a lazy load in async context
    return _out(g, 0)


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(group_id: uuid.UUID, ctx: Ctx = Depends(require("core.groups.read"))):
    g = await ctx.get(Group, group_id)
    n = await ctx.db.scalar(select(func.count()).where(GroupMember.group_id == g.id))
    return _out(g, n or 0)


@router.patch("/{group_id}", response_model=GroupOut)
async def update_group(group_id: uuid.UUID, body: GroupUpdate, ctx: Ctx = Depends(require("core.groups.write"))):
    g = await ctx.get(Group, group_id)
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(g, k, v)
    ctx.audit("core.group.update", g, changes)
    ctx.emit("core.group.updated", {"id": str(g.id), "name": g.name})
    await ctx.commit()
    return await get_group(group_id, ctx)


@router.delete("/{group_id}", status_code=204)
async def delete_group(group_id: uuid.UUID, ctx: Ctx = Depends(require("core.groups.write"))):
    g = await ctx.get(Group, group_id)
    removed = await remove_assignments_for(ctx, TargetType.GROUP, g.id)
    ctx.audit("core.group.delete", g, {"name": g.name, "removed_assignments": removed})
    ctx.emit("core.group.deleted", {"id": str(g.id), "name": g.name})
    if removed:
        emit_policy_changed(ctx, group_id=g.id)
    await ctx.db.delete(g)
    await ctx.commit()


@router.get("/{group_id}/members", response_model=list[MemberOut])
async def list_members(group_id: uuid.UUID, ctx: Ctx = Depends(require("core.groups.read"))):
    await ctx.get(Group, group_id)
    rows = await ctx.db.execute(
        select(User).join(GroupMember, GroupMember.user_id == User.id).where(GroupMember.group_id == group_id).order_by(User.username)
    )
    return list(rows.scalars())


@router.put("/{group_id}/members", response_model=list[MemberOut])
async def set_members(group_id: uuid.UUID, user_ids: list[uuid.UUID], ctx: Ctx = Depends(require("core.groups.write"))):
    """Replace the member list (handy for directory sync and CSV import)."""
    g = await ctx.get(Group, group_id)
    wanted = set(user_ids)
    found = set()
    if wanted:
        rows = await ctx.db.execute(select(User.id).where(User.org_id == ctx.org_id, User.id.in_(wanted)))
        found = set(rows.scalars())
    if missing := wanted - found:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown users: {sorted(map(str, missing))}")
    before = set((await ctx.db.execute(select(GroupMember.user_id).where(GroupMember.group_id == g.id))).scalars())
    await ctx.db.execute(delete(GroupMember).where(GroupMember.group_id == g.id))
    ctx.db.add_all(GroupMember(org_id=ctx.org_id, group_id=g.id, user_id=u) for u in wanted)
    ctx.audit("core.group.members", g, {"user_ids": sorted(map(str, wanted))})
    ctx.emit("core.group.members_changed", {"id": str(g.id)})
    if before != wanted:  # group-targeted policies now apply to different users
        emit_policy_changed(ctx, group_id=g.id)
    await ctx.commit()
    return await list_members(group_id, ctx)


@router.get("/{group_id}/teachers", response_model=list[MemberOut])
async def list_teachers(group_id: uuid.UUID, ctx: Ctx = Depends(require("core.groups.read"))):
    g = await ctx.get(Group, group_id)
    return g.teachers


@router.put("/{group_id}/teachers", response_model=list[MemberOut])
async def set_teachers(group_id: uuid.UUID, user_ids: list[uuid.UUID], ctx: Ctx = Depends(require("core.groups.write"))):
    """Replace the group's class teachers. Only users with the teacher role qualify.
    Class teachers can issue new one-time passwords to the group's students."""
    g = await ctx.get(Group, group_id)
    wanted = set(user_ids)
    if wanted:
        rows = await ctx.db.execute(
            select(User.id)
            .where(User.org_id == ctx.org_id, User.id.in_(wanted))
            .where(User.roles.any(RoleAssignment.role == "teacher"))
        )
        if invalid := wanted - set(rows.scalars()):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"not teachers (or unknown users): {sorted(map(str, invalid))}"
            )
    await ctx.db.execute(delete(GroupTeacher).where(GroupTeacher.group_id == g.id))
    ctx.db.add_all(GroupTeacher(org_id=ctx.org_id, group_id=g.id, user_id=u) for u in wanted)
    ctx.audit("core.group.teachers", g, {"user_ids": sorted(map(str, wanted))})
    ctx.emit("core.group.teachers_changed", {"id": str(g.id)})
    await ctx.commit()
    await ctx.db.refresh(g, ["teachers"])
    return g.teachers
