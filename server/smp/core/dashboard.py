"""Home-page cards contributed by the core."""

from datetime import timedelta

from sqlalchemy import func, select

from smp.core.context import Ctx
from smp.core.db import utcnow
from smp.core.models import AuditLog, Device, Group, PolicyAssignment, RoleAssignment, Room, User
from smp.core.module import Widget, WidgetItem

ONLINE_WINDOW = timedelta(minutes=5)
ROLE_LABELS = {"admin": "admin", "teacher": "tanár", "student": "diák"}


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
        out.append(Widget(id="core.rooms", title="Termek", value=await count(stmt), icon="🏢", link="/rooms", order=10))

    if p.can_anywhere("core.devices.read"):
        base = select(func.count()).select_from(Device).where(Device.org_id == ctx.org_id)
        if (allowed := p.rooms_with("core.devices.read")) is not None:
            base = base.where(Device.room_id.in_(allowed))
        total = await count(base)
        online = await count(base.where(Device.last_seen_at >= now - ONLINE_WINDOW))
        hint = f"{online} online"
        if p.can("core.devices.read"):
            unassigned = await count(base.where(Device.room_id.is_(None)))
            if unassigned:
                hint += f" · {unassigned} terem nélkül"
        out.append(Widget(id="core.devices", title="Gépek", value=total, hint=hint, icon="💻", link="/devices", order=20))

    if p.can("core.users.read"):
        total = await count(select(func.count()).select_from(User).where(User.org_id == ctx.org_id))
        rows = await ctx.db.execute(
            select(RoleAssignment.role, func.count(func.distinct(RoleAssignment.user_id)))
            .where(RoleAssignment.org_id == ctx.org_id)
            .group_by(RoleAssignment.role)
        )
        by_role = dict(rows.all())
        hint = " · ".join(f"{by_role[r]} {label}" for r, label in ROLE_LABELS.items() if by_role.get(r))
        out.append(Widget(id="core.users", title="Felhasználók", value=total, hint=hint, icon="🎓", link="/users", order=30))

    if p.can("core.groups.read"):
        n = await count(select(func.count()).select_from(Group).where(Group.org_id == ctx.org_id))
        out.append(Widget(id="core.groups", title="Csoportok", value=n, icon="👥", link="/groups", order=40))

    if p.can_anywhere("core.policies.read"):
        n = await count(
            select(func.count())
            .select_from(PolicyAssignment)
            .where(
                PolicyAssignment.org_id == ctx.org_id,
                PolicyAssignment.enabled.is_(True),
                PolicyAssignment.ends_at > now,
                (PolicyAssignment.starts_at.is_(None)) | (PolicyAssignment.starts_at <= now),
            )
        )
        out.append(
            Widget(id="core.temporary_policies", title="Ideiglenes szabályok", value=n, hint="pl. vizsgamód",
                   icon="📋", link="/policies?tab=assignments", order=50)
        )

    if p.can("core.audit.read"):
        failed = await count(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.org_id == ctx.org_id, AuditLog.action == "core.auth.login_failed", AuditLog.at >= now - timedelta(hours=24))
        )
        out.append(
            Widget(id="core.failed_logins", title="Sikertelen belépések", value=failed, hint="utolsó 24 óra",
                   icon="🔒", link="/audit?action=core.auth.login_failed", order=60)
        )
        recent = await ctx.db.execute(
            select(AuditLog).where(AuditLog.org_id == ctx.org_id).order_by(AuditLog.id.desc()).limit(8)
        )
        out.append(
            Widget(
                id="core.recent_activity",
                kind="list",
                title="Legutóbbi események",
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
