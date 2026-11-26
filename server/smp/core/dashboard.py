# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Home-page cards contributed by the core."""

from datetime import timedelta

from sqlalchemy import ColumnElement, exists, func, or_, select

from smp.core.context import Ctx, Principal
from smp.core.db import utcnow
from smp.core.models import AuditLog, Device, Group, PolicyAssignment, RoleAssignment, Room, User
from smp.core.module import Widget, WidgetItem
from smp.core.policy import assignment_scope

# A device is online while its agent reports; agents check in at least every ~25 s
# (long poll), so 90 s without a heartbeat means it's off or off the network.
# The web UI uses the same window (WEBUI/src/format.ts, isOnline).
ONLINE_WINDOW = timedelta(seconds=90)
ROLE_LABELS = {
    "admin": ("admin", "admins"),
    "unit_manager": ("unit manager", "unit managers"),
    "manager": ("manager", "managers"),
    "teacher": ("teacher", "teachers"),
    "student": ("student", "students"),
}


def audit_in_scope(principal: Principal) -> ColumnElement[bool] | None:
    """Which audit entries the principal may read: None = all (org-wide grant);
    otherwise a condition on AuditLog.building_ids (entries about their buildings;
    org-level entries have NULL there and never match)."""
    buildings = principal.buildings_with("core.audit.read")
    if buildings is None:
        return None
    return AuditLog.building_ids.overlap(list(buildings))


async def core_widgets(ctx: Ctx) -> list[Widget]:
    p = ctx.principal
    assert p is not None
    now = utcnow()
    out: list[Widget] = []

    async def count(stmt) -> int:
        return (await ctx.db.scalar(stmt)) or 0

    if p.can_anywhere("core.rooms.read"):
        stmt = select(func.count()).select_from(Room).where(Room.org_id == ctx.org_id)
        if (allowed := p.rooms_with("core.rooms.read")) is not None:
            stmt = stmt.where(Room.id.in_(allowed))
        out.append(Widget(id="core.rooms", title="Rooms", value=await count(stmt), icon="🏢", link="/rooms", order=10))

    if p.can_anywhere("core.devices.read"):
        base = select(func.count()).select_from(Device).where(Device.org_id == ctx.org_id)
        if (allowed := p.rooms_with("core.devices.read")) is not None:
            base = base.where(Device.room_id.in_(allowed))
        total = await count(base)
        online = await count(base.where(Device.last_seen_at >= now - ONLINE_WINDOW))
        hint = f"{online} online"
        data: dict[str, int] = {"online": online}
        if p.can("core.devices.read"):
            unassigned = await count(base.where(Device.room_id.is_(None)))
            data["unassigned"] = unassigned
            if unassigned:
                hint += f" · {unassigned} without a room"
        out.append(
            Widget(id="core.devices", title="Devices", value=total, hint=hint, data=data, icon="💻", link="/devices", order=20)
        )

    if p.can_anywhere("core.users.read"):
        users_q = select(func.count()).select_from(User).where(User.org_id == ctx.org_id)
        roles_q = select(RoleAssignment.role, func.count(func.distinct(RoleAssignment.user_id))).where(
            RoleAssignment.org_id == ctx.org_id
        )
        if (mine := p.buildings_with("core.users.read")) is not None:
            # A building manager counts the users of their buildings: those with a role
            # scoped to the building or to one of its rooms.
            b = list(mine)
            in_building = or_(
                RoleAssignment.building_id.in_(b),
                RoleAssignment.room_id.in_(select(Room.id).where(Room.building_id.in_(b))),
            )
            users_q = users_q.where(exists().where(RoleAssignment.user_id == User.id, in_building))
            roles_q = roles_q.where(in_building)
        total = await count(users_q)
        rows = await ctx.db.execute(roles_q.group_by(RoleAssignment.role))
        by_role = dict(rows.all())
        hint = " · ".join(
            f"{by_role[r]} {one if by_role[r] == 1 else many}" for r, (one, many) in ROLE_LABELS.items() if by_role.get(r)
        )
        data = {r: by_role.get(r, 0) for r in ROLE_LABELS}
        out.append(
            Widget(id="core.users", title="Users", value=total, hint=hint, data=data, icon="🎓", link="/users", order=30)
        )

    if p.can("core.groups.read"):
        n = await count(select(func.count()).select_from(Group).where(Group.org_id == ctx.org_id))
        out.append(Widget(id="core.groups", title="Groups", value=n, icon="👥", link="/groups", order=40))

    if p.can_anywhere("core.policies.read"):
        temp_q = (
            select(func.count())
            .select_from(PolicyAssignment)
            .where(
                PolicyAssignment.org_id == ctx.org_id,
                PolicyAssignment.enabled.is_(True),
                PolicyAssignment.ends_at > now,
                (PolicyAssignment.starts_at.is_(None)) | (PolicyAssignment.starts_at <= now),
            )
        )
        if (listable := assignment_scope(p)) is not None:  # only what GET /policy-assignments would list
            temp_q = temp_q.where(listable)
        n = await count(temp_q)
        out.append(
            Widget(id="core.temporary_policies", title="Temporary policies", value=n, hint="e.g. exam mode",
                   icon="📋", link="/policies?tab=assignments", order=50)
        )

    if p.can_anywhere("core.audit.read"):
        in_scope = audit_in_scope(p)
        if in_scope is None:  # org-level entries (failed logins) are for org-wide readers only
            failed = await count(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.org_id == ctx.org_id,
                    AuditLog.action == "core.auth.login_failed",
                    AuditLog.at >= now - timedelta(hours=24),
                )
            )
            out.append(
                Widget(id="core.failed_logins", title="Failed logins", value=failed, hint="last 24 hours",
                       icon="🔒", link="/audit?action=core.auth.login_failed", order=60)
            )
        recent_q = select(AuditLog).where(AuditLog.org_id == ctx.org_id)
        if in_scope is not None:
            recent_q = recent_q.where(in_scope)
        recent = await ctx.db.execute(recent_q.order_by(AuditLog.id.desc()).limit(8))
        out.append(
            Widget(
                id="core.recent_activity",
                kind="list",
                title="Recent activity",
                icon="📜",
                link="/audit",
                order=200,
                items=[
                    WidgetItem(label=e.action, value=e.actor_name or e.actor_type, at=e.at,
                               link=f"/audit?action={e.action}")
                    for e in recent.scalars()
                ],
            )
        )
    return out
