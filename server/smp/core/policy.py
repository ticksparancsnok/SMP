# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Policy resolution: which policy of each kind applies to a device/user right now.

Rules (documented in docs/architecture.md):
  1. Candidate assignments are those targeting the org, the building of the device's
     room, the device's room, the device itself, the user, or any of the user's groups.
  2. An assignment counts only if enabled, inside [starts_at, ends_at) and, when it
     has a schedule, inside one of the schedule's windows.
  3. Per kind, the winner is the highest `priority`; ties go to the more specific
     target (device/user > room/group > building > org); remaining ties to the newest.

So the default policy is an org assignment with priority 0, a room's lesson-time
policy is a room assignment on a schedule, and "exam mode" is a room assignment with
priority 100 and an ends_at.
"""

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime, time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, delete, exists, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from smp.core.db import utcnow
from smp.core.models import (
    Device,
    GroupBuilding,
    GroupMember,
    Policy,
    PolicyAssignment,
    RoleAssignment,
    Room,
    Schedule,
    TargetType,
)

if TYPE_CHECKING:
    from smp.core.context import Ctx, Principal

SPECIFICITY = {
    TargetType.ORG: 0,
    TargetType.BUILDING: 1,
    TargetType.ROOM: 2,
    TargetType.GROUP: 2,
    TargetType.DEVICE: 3,
    TargetType.USER: 3,
}


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def validate_windows(windows: list[dict[str, Any]]) -> None:
    """Raise ValueError (-> 422) for anything malformed. Other exception types would
    escape pydantic and surface as a 500."""
    for w in windows:
        if not isinstance(w, dict):
            raise ValueError("each window must be an object")
        days = w.get("days")
        if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(7) for d in days):
            raise ValueError("window.days must be a non-empty list of 0..6 (0 = Monday)")
        if not isinstance(w.get("start"), str) or not isinstance(w.get("end"), str):
            raise ValueError("window must have 'start' and 'end' as \"HH:MM\" strings")
        try:
            start, end = _parse_hhmm(w["start"]), _parse_hhmm(w["end"])
        except ValueError as e:
            raise ValueError(f"invalid time, expected \"HH:MM\": {e}") from e
        if start == end:
            raise ValueError("window start and end must differ")


def schedule_active(schedule: Schedule, at: datetime) -> bool:
    """Windows may cross midnight (start > end), e.g. 22:00-06:00."""
    local = at.astimezone(ZoneInfo(schedule.timezone))
    wd, t = local.weekday(), local.time()
    for w in schedule.windows:
        start, end = _parse_hhmm(w["start"]), _parse_hhmm(w["end"])
        if start < end:
            if wd in w["days"] and start <= t < end:
                return True
        else:  # crosses midnight: the window "belongs" to the day it starts on
            if (wd in w["days"] and t >= start) or ((wd - 1) % 7 in w["days"] and t < end):
                return True
    return False


@dataclass(frozen=True)
class EffectivePolicy:
    kind: str
    policy: Policy
    assignment: PolicyAssignment

    def as_dict(self) -> dict[str, Any]:
        a = self.assignment
        return {
            "kind": self.kind,
            "policy_id": str(self.policy.id),
            "policy_name": self.policy.name,
            "data": self.policy.data,
            "source": {
                "assignment_id": str(a.id),
                "target_type": a.target_type,
                "target_id": str(a.target_id) if a.target_id else None,
                "priority": a.priority,
                "label": a.label,
                "ends_at": a.ends_at.isoformat() if a.ends_at else None,
            },
        }


async def effective_policies(
    db: AsyncSession,
    org_id: uuid.UUID,
    *,
    device: Device | None = None,
    room_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    kinds: set[str] | None = None,
    at: datetime | None = None,
) -> dict[str, EffectivePolicy]:
    at = at or utcnow()
    if device is not None:
        room_id = room_id or device.room_id

    target_filters = [PolicyAssignment.target_type == TargetType.ORG]
    if room_id:
        target_filters.append((PolicyAssignment.target_type == TargetType.ROOM) & (PolicyAssignment.target_id == room_id))
        # The room's building, looked up inside the same query (NULL matches nothing).
        building_id = select(Room.building_id).where(Room.id == room_id).scalar_subquery()
        target_filters.append(
            (PolicyAssignment.target_type == TargetType.BUILDING) & (PolicyAssignment.target_id == building_id)
        )
    if device is not None:
        target_filters.append(
            (PolicyAssignment.target_type == TargetType.DEVICE) & (PolicyAssignment.target_id == device.id)
        )
    if user_id:
        target_filters.append((PolicyAssignment.target_type == TargetType.USER) & (PolicyAssignment.target_id == user_id))
        group_ids = select(GroupMember.group_id).where(GroupMember.user_id == user_id)
        target_filters.append(
            (PolicyAssignment.target_type == TargetType.GROUP) & (PolicyAssignment.target_id.in_(group_ids))
        )

    q = (
        select(PolicyAssignment)
        .join(Policy, Policy.id == PolicyAssignment.policy_id)
        .options(contains_eager(PolicyAssignment.policy))  # reuse this join for .policy
        .where(
            PolicyAssignment.org_id == org_id,
            PolicyAssignment.enabled.is_(True),
            or_(PolicyAssignment.starts_at.is_(None), PolicyAssignment.starts_at <= at),
            or_(PolicyAssignment.ends_at.is_(None), PolicyAssignment.ends_at > at),
            or_(*target_filters),
        )
    )
    if kinds:
        q = q.where(Policy.kind.in_(kinds))
    assignments = list((await db.execute(q)).scalars().unique())

    schedule_ids = {a.schedule_id for a in assignments if a.schedule_id}
    schedules: dict[uuid.UUID, Schedule] = {}
    if schedule_ids:
        rows = await db.execute(select(Schedule).where(Schedule.id.in_(schedule_ids)))
        schedules = {s.id: s for s in rows.scalars()}

    best: dict[str, EffectivePolicy] = {}
    for a in assignments:
        if a.schedule_id:
            sched = schedules.get(a.schedule_id)
            if sched is None or not schedule_active(sched, at):
                continue
        kind = a.policy.kind
        cur = best.get(kind)
        if cur is None or _rank(a) > _rank(cur.assignment):
            best[kind] = EffectivePolicy(kind, a.policy, a)
    return best


def _rank(a: PolicyAssignment) -> tuple[int, int, datetime]:
    return (a.priority, SPECIFICITY[TargetType(a.target_type)], a.created_at)


async def resolve_ip(db: AsyncSession, org_id: uuid.UUID, ip: str) -> tuple[Device | None, Room | None]:
    """Map a client IP to (device, room). Device match uses the agent-reported
    last_ip; otherwise the room whose subnet contains the IP (longest prefix wins)."""
    addr = ipaddress.ip_address(ip)
    device = (
        await db.execute(
            select(Device)
            .where(Device.org_id == org_id, Device.last_ip == str(addr))
            .order_by(Device.last_seen_at.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()
    if device is not None and device.room_id is not None:
        return device, await db.get(Room, device.room_id)
    return device, await room_for_ip(db, org_id, str(addr))


async def room_for_ip(db: AsyncSession, org_id: uuid.UUID, ip: str) -> Room | None:
    """The room whose subnets contain `ip` (longest prefix wins), or None. Used for
    devices without an agent, and for agents whose room follows the network."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    best: tuple[int, Room] | None = None
    for room in (await db.execute(select(Room).where(Room.org_id == org_id))).scalars():
        for cidr in room.subnets or []:
            net = ipaddress.ip_network(cidr, strict=False)
            if addr.version == net.version and addr in net and (best is None or net.prefixlen > best[0]):
                best = (net.prefixlen, room)
    return best[1] if best else None


# --------------------------------------------------------------------------- scope of targets


def group_within(group_id: Any, buildings: set[uuid.UUID]) -> ColumnElement[bool]:
    """SQL condition: the group belongs to at least one building and all of its
    buildings are in `buildings` (a school-wide group is in none). `group_id` is a
    value or a column of an enclosing query."""
    has_any = exists().where(GroupBuilding.group_id == group_id)
    outside = exists().where(GroupBuilding.group_id == group_id, GroupBuilding.building_id.not_in(list(buildings)))
    return and_(has_any, not_(outside))


def user_within(user_id: Any, buildings: set[uuid.UUID]) -> ColumnElement[bool]:
    """SQL condition: the user has at least one role assignment and every one of them
    is scoped to one of `buildings` or to a room in one of them (an org-wide
    assignment is outside any building)."""
    b = list(buildings)
    rooms_in = select(Room.id).where(Room.building_id.in_(b))
    has_any = exists().where(RoleAssignment.user_id == user_id)
    inside = or_(
        and_(RoleAssignment.building_id.is_not(None), RoleAssignment.building_id.in_(b)),
        and_(RoleAssignment.room_id.is_not(None), RoleAssignment.room_id.in_(rooms_in)),
    )
    outside = exists().where(RoleAssignment.user_id == user_id, not_(inside))
    return and_(has_any, not_(outside))


def assignment_scope(principal: "Principal") -> ColumnElement[bool] | None:
    """Which policy assignments the principal may list: None = all (org-wide read);
    otherwise a condition on PolicyAssignment. The org's own assignments are visible to
    every reader; the others only when their target lies in the reader's rooms or
    buildings (a group or user by the same rule as for assigning: see group_within)."""
    buildings = principal.buildings_with("core.policies.read")
    rooms = principal.rooms_with("core.policies.read")
    if buildings is None or rooms is None:
        return None
    t, tid = PolicyAssignment.target_type, PolicyAssignment.target_id
    return or_(
        t == TargetType.ORG,
        and_(t == TargetType.BUILDING, tid.in_(list(buildings))),
        and_(t == TargetType.ROOM, tid.in_(list(rooms))),
        and_(t == TargetType.DEVICE, tid.in_(select(Device.id).where(Device.room_id.in_(list(rooms))))),
        and_(t == TargetType.GROUP, group_within(tid, buildings)),
        and_(t == TargetType.USER, user_within(tid, buildings)),
    )


# --------------------------------------------------------------------------- change helpers


def emit_policy_changed(ctx: "Ctx", **payload: Any) -> None:
    """One subject for every change that can alter effective policies (assignments,
    schedules, a device moving rooms, room subnets, group membership, ...). DNS,
    agents etc. subscribe to it and refresh their cached view."""
    ctx.emit("core.policy.changed", {k: str(v) if v is not None else None for k, v in payload.items()})


async def remove_assignments_for(ctx: "Ctx", target_type: TargetType, target_id: uuid.UUID) -> int:
    """Delete assignments that target an entity being deleted. target_id has no FK
    (it points at different tables), so the database can't cascade this for us."""
    result = await ctx.db.execute(
        delete(PolicyAssignment).where(
            PolicyAssignment.org_id == ctx.org_id,
            PolicyAssignment.target_type == target_type,
            PolicyAssignment.target_id == target_id,
        )
    )
    return result.rowcount or 0
