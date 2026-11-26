# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from smp.core import roles as role_table
from smp.core.api import ORMModel
from smp.core.api.users import group_visible, users_in_buildings, visible_user_buildings
from smp.core.context import Ctx, require
from smp.core.errors import AppError
from smp.core.models import (
    Building,
    Group,
    GroupBuilding,
    GroupMember,
    GroupTeacher,
    RoleAssignment,
    TargetType,
    User,
)
from smp.core.policy import emit_policy_changed, remove_assignments_for

router = APIRouter(prefix="/groups", tags=["groups"])

GROUPS_READ = "core.groups.read"
GROUPS_WRITE = "core.groups.write"


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field("custom", pattern=r"^(class|staff|custom)$")
    description: str = ""
    # The buildings the group belongs to; empty = school-wide (only for those who may
    # edit the whole school; a manager's default is all of their buildings).
    building_ids: list[uuid.UUID] | None = None


class GroupUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    kind: str | None = Field(None, pattern=r"^(class|staff|custom)$")
    description: str | None = None
    building_ids: list[uuid.UUID] | None = None  # replaces the group's buildings


class GroupOut(ORMModel):
    id: uuid.UUID
    name: str
    kind: str
    description: str
    external_id: str | None
    building_ids: list[uuid.UUID] = []  # empty = school-wide
    member_count: int = 0
    teachers: list["MemberOut"] = []  # class teachers (may reset their students' passwords)


class MemberOut(ORMModel):
    id: uuid.UUID
    username: str
    display_name: str


GroupOut.model_rebuild()


def _out(g: Group, count: int, seen_teachers: set[uuid.UUID] | None = None) -> GroupOut:
    """`seen_teachers`: the teacher ids the caller may see (None = all of them)."""
    out = GroupOut.model_validate(g)
    out.building_ids = sorted(b.id for b in g.buildings)
    out.member_count = count
    if seen_teachers is not None:
        out.teachers = [t for t in out.teachers if t.id in seen_teachers]
    return out


def _buildings(g: Group) -> set[uuid.UUID]:
    return {b.id for b in g.buildings}


class _Access:
    """Which groups the caller may see and edit. read_b / write_b: the buildings of
    core.groups.read / core.groups.write (None = the whole school). A group belongs to
    the buildings in group_buildings; one with none is school-wide, which only an
    org-wide caller reaches. Editing needs *all* of the group's buildings to be the
    caller's."""

    def __init__(self, ctx: Ctx):
        p = ctx.principal
        assert p is not None
        self.ctx = ctx
        self.read_b = p.buildings_with(GROUPS_READ)
        self.write_b = p.buildings_with(GROUPS_WRITE)
        self.p = p
        # The buildings whose users the caller sees (None = all users): what a limited
        # caller may count, list, and add to a group.
        self.users_vis = visible_user_buildings(p)

    def visible(self, g: Group) -> bool:
        return group_visible(self.p, g)

    def editable(self, g: Group) -> bool:
        if self.write_b is None:
            return True
        b = _buildings(g)
        return bool(b) and b <= self.write_b

    def visible_members(self):
        """SQL condition on GroupMember: the member is a user the caller may see (None =
        no restriction)."""
        if self.users_vis is None:
            return None
        return GroupMember.user_id.in_(select(User.id).where(users_in_buildings(self.users_vis)))

    async def seen_teachers(self, groups: list[Group]) -> set[uuid.UUID] | None:
        """The ids of the groups' teachers that the caller may see (None = all)."""
        if self.users_vis is None:
            return None
        ids = {t.id for g in groups for t in g.teachers}
        if not ids:
            return set()
        rows = await self.ctx.db.execute(select(User.id).where(User.id.in_(ids), users_in_buildings(self.users_vis)))
        return set(rows.scalars()) | ({self.p.id} & ids if self.p.kind == "user" else set())

    async def load(self, group_id: uuid.UUID, *, edit: bool = False) -> Group:
        """The group, or 404 when the caller can't see it, or 403 when they may see but
        not edit it."""
        g = await self.ctx.get(Group, group_id)
        if not self.visible(g):
            raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Group not found", entity="Group")
        if edit and not self.editable(g):
            raise _out_of_scope()
        return g

    async def check_users(self, user_ids: set[uuid.UUID]) -> None:
        """Users a limited caller adds to a group must be ones they can see."""
        if self.users_vis is None or not user_ids:
            return
        rows = await self.ctx.db.execute(select(User.id).where(User.id.in_(user_ids), users_in_buildings(self.users_vis)))
        if bad := user_ids - set(rows.scalars()):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown users: {sorted(map(str, bad))}")

    async def not_visible(self, user_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        """Of these users, the ones a limited caller can't see (they keep their place in
        a group the caller edits: it is not theirs to remove)."""
        if self.users_vis is None or not user_ids:
            return set()
        rows = await self.ctx.db.execute(select(User.id).where(User.id.in_(user_ids), users_in_buildings(self.users_vis)))
        return user_ids - set(rows.scalars())

    async def buildings_for(self, wanted: list[uuid.UUID] | None) -> set[uuid.UUID]:
        """Validate a requested set of buildings for a new or changed group. A limited
        caller needs a non-empty subset of their own (None = all of them)."""
        if self.write_b is not None:
            chosen = set(wanted) if wanted is not None else set(self.write_b)
            if not chosen or not chosen <= self.write_b:
                raise _out_of_scope()
        else:
            chosen = set(wanted or ())
        if chosen:
            rows = await self.ctx.db.execute(
                select(Building.id).where(Building.org_id == self.ctx.org_id, Building.id.in_(chosen))
            )
            if missing := chosen - set(rows.scalars()):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown buildings: {sorted(map(str, missing))}")
        return chosen


def _out_of_scope() -> AppError:
    return AppError(
        status.HTTP_403_FORBIDDEN, "groups.out_of_scope", "that group or building is outside the buildings you manage"
    )


def _building_ids(*sets: set[uuid.UUID]) -> list[uuid.UUID] | None:
    union = set().union(*sets)
    return sorted(union) if union else None


async def _set_buildings(ctx: Ctx, g: Group, wanted: set[uuid.UUID]) -> bool:
    """Replace the group's buildings; True when they changed."""
    current = _buildings(g)
    if current == wanted:
        return False
    await ctx.db.execute(delete(GroupBuilding).where(GroupBuilding.group_id == g.id))
    ctx.db.add_all(GroupBuilding(org_id=ctx.org_id, group_id=g.id, building_id=b) for b in sorted(wanted))
    return True


@router.get("", response_model=list[GroupOut])
async def list_groups(ctx: Ctx = Depends(require(GROUPS_READ, any_room=True))):
    """Every group for an org-wide reader; otherwise the groups with a building among the caller's."""
    acc = _Access(ctx)
    counted = select(GroupMember.group_id, func.count().label("n")).where(GroupMember.org_id == ctx.org_id)
    if (seen := acc.visible_members()) is not None:
        counted = counted.where(seen)  # a limited reader only counts users they can see
    counts = counted.group_by(GroupMember.group_id).subquery()
    stmt = (
        ctx.select(Group, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.group_id == Group.id)
        .order_by(Group.name)
    )
    if acc.read_b is not None:
        in_buildings = select(GroupBuilding.group_id).where(GroupBuilding.building_id.in_(acc.read_b))
        stmt = stmt.where(Group.id.in_(in_buildings))
    found = (await ctx.db.execute(stmt)).all()
    seen_teachers = await acc.seen_teachers([g for g, _ in found])
    return [_out(g, n, seen_teachers) for g, n in found]


@router.post("", response_model=GroupOut, status_code=201)
async def create_group(body: GroupIn, ctx: Ctx = Depends(require(GROUPS_WRITE, any_room=True))):
    acc = _Access(ctx)
    buildings = await acc.buildings_for(body.building_ids)
    g = Group(org_id=ctx.org_id, **body.model_dump(exclude={"building_ids"}))
    ctx.db.add(g)
    await ctx.db.flush()
    ctx.db.add_all(GroupBuilding(org_id=ctx.org_id, group_id=g.id, building_id=b) for b in sorted(buildings))
    await ctx.db.flush()
    ctx.audit(
        "core.group.create",
        g,
        {**body.model_dump(exclude={"building_ids"}), "building_ids": sorted(map(str, buildings))},
        building_ids=_building_ids(buildings),
    )
    ctx.emit("core.group.created", {"id": str(g.id), "name": g.name})
    await ctx.commit()
    await ctx.db.refresh(g, ["teachers", "buildings"])  # avoid a lazy load in async context
    return _out(g, 0, await acc.seen_teachers([g]))


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(group_id: uuid.UUID, ctx: Ctx = Depends(require(GROUPS_READ, any_room=True))):
    acc = _Access(ctx)
    g = await acc.load(group_id)
    count = select(func.count()).where(GroupMember.group_id == g.id)
    if (seen := acc.visible_members()) is not None:
        count = count.where(seen)
    return _out(g, await ctx.db.scalar(count) or 0, await acc.seen_teachers([g]))


@router.patch("/{group_id}", response_model=GroupOut)
async def update_group(group_id: uuid.UUID, body: GroupUpdate, ctx: Ctx = Depends(require(GROUPS_WRITE, any_room=True))):
    acc = _Access(ctx)
    g = await acc.load(group_id, edit=True)
    changes = body.model_dump(exclude_unset=True, exclude={"building_ids"})
    before = _buildings(g)
    moved = False
    after = before
    if body.building_ids is not None:
        after = await acc.buildings_for(body.building_ids)
        moved = await _set_buildings(ctx, g, after)
        if moved:
            changes["building_ids"] = sorted(map(str, after))
    for k, v in changes.items():
        if k != "building_ids":
            setattr(g, k, v)
    ctx.audit("core.group.update", g, changes, building_ids=_building_ids(before, after))
    ctx.emit("core.group.updated", {"id": str(g.id), "name": g.name})
    if moved:
        emit_policy_changed(ctx, group_id=g.id)
    await ctx.commit()
    await ctx.db.refresh(g, ["buildings"])
    return await get_group(group_id, ctx)


@router.delete("/{group_id}", status_code=204)
async def delete_group(group_id: uuid.UUID, ctx: Ctx = Depends(require(GROUPS_WRITE, any_room=True))):
    g = await _Access(ctx).load(group_id, edit=True)
    removed = await remove_assignments_for(ctx, TargetType.GROUP, g.id)
    ctx.audit(
        "core.group.delete", g, {"name": g.name, "removed_assignments": removed}, building_ids=_building_ids(_buildings(g))
    )
    ctx.emit("core.group.deleted", {"id": str(g.id), "name": g.name})
    if removed:
        emit_policy_changed(ctx, group_id=g.id)
    await ctx.db.delete(g)
    await ctx.commit()


async def _members(ctx: Ctx, acc: _Access, group_id: uuid.UUID) -> list[User]:
    stmt = select(User).join(GroupMember, GroupMember.user_id == User.id).where(GroupMember.group_id == group_id)
    if acc.users_vis is not None:
        stmt = stmt.where(users_in_buildings(acc.users_vis))  # a limited reader only sees their own users
    return list((await ctx.db.execute(stmt.order_by(User.username))).scalars())


@router.get("/{group_id}/members", response_model=list[MemberOut])
async def list_members(group_id: uuid.UUID, ctx: Ctx = Depends(require(GROUPS_READ, any_room=True))):
    acc = _Access(ctx)
    await acc.load(group_id)
    return await _members(ctx, acc, group_id)


@router.put("/{group_id}/members", response_model=list[MemberOut])
async def set_members(
    group_id: uuid.UUID, user_ids: list[uuid.UUID], ctx: Ctx = Depends(require(GROUPS_WRITE, any_room=True))
):
    """Replace the member list (handy for directory sync and CSV import). A manager
    can only add users they can see, and members they can't see stay."""
    acc = _Access(ctx)
    g = await acc.load(group_id, edit=True)
    wanted = set(user_ids)
    found = set()
    if wanted:
        rows = await ctx.db.execute(select(User.id).where(User.org_id == ctx.org_id, User.id.in_(wanted)))
        found = set(rows.scalars())
    if missing := wanted - found:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown users: {sorted(map(str, missing))}")
    await acc.check_users(wanted)
    before = set((await ctx.db.execute(select(GroupMember.user_id).where(GroupMember.group_id == g.id))).scalars())
    wanted |= await acc.not_visible(before)  # not the caller's to remove
    await ctx.db.execute(delete(GroupMember).where(GroupMember.group_id == g.id))
    ctx.db.add_all(GroupMember(org_id=ctx.org_id, group_id=g.id, user_id=u) for u in wanted)
    ctx.audit("core.group.members", g, {"user_ids": sorted(map(str, wanted))}, building_ids=_building_ids(_buildings(g)))
    ctx.emit("core.group.members_changed", {"id": str(g.id)})
    if before != wanted:  # group-targeted policies now apply to different users
        emit_policy_changed(ctx, group_id=g.id)
    await ctx.commit()
    return await _members(ctx, acc, group_id)


@router.get("/{group_id}/teachers", response_model=list[MemberOut])
async def list_teachers(group_id: uuid.UUID, ctx: Ctx = Depends(require(GROUPS_READ, any_room=True))):
    acc = _Access(ctx)
    g = await acc.load(group_id)
    seen = await acc.seen_teachers([g])
    return [t for t in g.teachers if seen is None or t.id in seen]


@router.put("/{group_id}/teachers", response_model=list[MemberOut])
async def set_teachers(
    group_id: uuid.UUID, user_ids: list[uuid.UUID], ctx: Ctx = Depends(require(GROUPS_WRITE, any_room=True))
):
    """Replace the group's class teachers. Only users holding a role that includes the
    teacher's powers qualify (teacher, manager, unit manager). Class teachers can issue
    new one-time passwords to the group's students. A manager can only name users they
    can see, and teachers they can't see stay."""
    acc = _Access(ctx)
    g = await acc.load(group_id, edit=True)
    wanted = set(user_ids)
    if wanted:
        teacher_roles = [r for r in role_table.ROLE_DEFS if "teacher" in role_table.with_inherited(r)]
        rows = await ctx.db.execute(
            select(User.id)
            .where(User.org_id == ctx.org_id, User.id.in_(wanted))
            .where(User.roles.any(RoleAssignment.role.in_(teacher_roles)))
        )
        if invalid := wanted - set(rows.scalars()):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"not teachers (or unknown users): {sorted(map(str, invalid))}"
            )
        await acc.check_users(wanted)
    wanted |= await acc.not_visible({t.id for t in g.teachers})  # not the caller's to remove
    await ctx.db.execute(delete(GroupTeacher).where(GroupTeacher.group_id == g.id))
    ctx.db.add_all(GroupTeacher(org_id=ctx.org_id, group_id=g.id, user_id=u) for u in wanted)
    ctx.audit("core.group.teachers", g, {"user_ids": sorted(map(str, wanted))}, building_ids=_building_ids(_buildings(g)))
    ctx.emit("core.group.teachers_changed", {"id": str(g.id)})
    await ctx.commit()
    await ctx.db.refresh(g, ["teachers"])
    seen = await acc.seen_teachers([g])
    return [t for t in g.teachers if seen is None or t.id in seen]
