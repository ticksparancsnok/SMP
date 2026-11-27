# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Which packages a computer should have, and how its reports read against that.

Desired state: every assignment that targets the whole school, the computer's building
(the one its room is in), its room or the computer itself counts; per package the most
specific one decides (computer > room > building > school). So "install Chrome
everywhere" plus "remove Chrome on LAB-PC07" means LAB-PC07 removes it, and every other
computer installs it.

A package nobody assigns any more is left alone on the computers: only a "remove"
assignment uninstalls. Laptops that move between rooms ("by network" devices) would
otherwise uninstall and reinstall software on every move, and a deleted assignment
would silently wipe a program from a whole school.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smp.core.models import Room
from smp.modules.software.models import SoftwareAssignment, SoftwareDeviceState

SPECIFICITY = {"org": 0, "building": 1, "room": 2, "device": 3}

# What agents report (POST /agent/status).
WAITING = "waiting"  # spread delay, or every download slot is busy
REPORTED_STATES = (
    WAITING, "downloading", "installing", "installed", "reboot_pending", "failed",
    "removing", "removed", "remove_failed",
)
IN_PROGRESS = frozenset({WAITING, "downloading", "installing", "removing"})
FAILED = frozenset({"failed", "remove_failed"})
# Not reported: the computer hasn't said anything about the current file yet.
PENDING = "pending"

_INSTALL_STATES = frozenset({WAITING, "downloading", "installing", "installed", "reboot_pending", "failed"})
_REMOVE_STATES = frozenset({WAITING, "removing", "removed", "remove_failed"})


@dataclass(frozen=True)
class Target:
    package_id: uuid.UUID
    target_type: str
    building_id: uuid.UUID | None
    room_id: uuid.UUID | None
    device_id: uuid.UUID | None
    action: str


def pick(
    targets: Iterable[Target], device_id: uuid.UUID, room_id: uuid.UUID | None, building_id: uuid.UUID | None = None
) -> dict[uuid.UUID, str]:
    """package id -> "install" | "remove" for one computer (most specific target wins).
    building_id: the building of the computer's room (None: no room, or a room in no building)."""
    best: dict[uuid.UUID, tuple[int, str]] = {}
    for t in targets:
        applies = (
            t.target_type == "org"
            or (t.target_type == "building" and building_id is not None and t.building_id == building_id)
            or (t.target_type == "room" and room_id is not None and t.room_id == room_id)
            or (t.target_type == "device" and t.device_id == device_id)
        )
        if not applies:
            continue
        rank = SPECIFICITY[t.target_type]
        if t.package_id not in best or rank > best[t.package_id][0]:
            best[t.package_id] = (rank, t.action)
    return {pkg: action for pkg, (_, action) in best.items()}


def _targets(rows) -> list[Target]:
    return [Target(*row) for row in rows]


_COLUMNS = (
    SoftwareAssignment.package_id, SoftwareAssignment.target_type, SoftwareAssignment.building_id,
    SoftwareAssignment.room_id, SoftwareAssignment.device_id, SoftwareAssignment.action,
)


async def desired_for_device(
    db: AsyncSession, org_id: uuid.UUID, device_id: uuid.UUID, room_id: uuid.UUID | None
) -> dict[uuid.UUID, str]:
    """One computer's packages (the agent config, its downloads). Two small queries (the
    building comes from the room)."""
    where = [SoftwareAssignment.target_type == "org", SoftwareAssignment.device_id == device_id]
    building_id = None
    if room_id is not None:
        where.append(SoftwareAssignment.room_id == room_id)
        building_id = await db.scalar(select(Room.building_id).where(Room.org_id == org_id, Room.id == room_id))
        if building_id is not None:
            where.append(SoftwareAssignment.building_id == building_id)
    rows = await db.execute(select(*_COLUMNS).where(SoftwareAssignment.org_id == org_id, or_(*where)))
    return pick(_targets(rows), device_id, room_id, building_id)


async def desired_for_devices(
    db: AsyncSession, org_id: uuid.UUID, devices: Iterable[tuple[uuid.UUID, uuid.UUID | None]],
    package_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, dict[uuid.UUID, str]]:
    """Many computers at once ((device id, room id) pairs): the status pages. Loads the
    school's assignments (and which building each room is in) once and resolves in memory."""
    stmt = select(*_COLUMNS).where(SoftwareAssignment.org_id == org_id)
    if package_id is not None:
        stmt = stmt.where(SoftwareAssignment.package_id == package_id)
    targets = _targets(await db.execute(stmt))
    building_of = dict((await db.execute(
        select(Room.id, Room.building_id).where(Room.org_id == org_id, Room.building_id.is_not(None))
    )).all())
    return {device_id: pick(targets, device_id, room_id, building_of.get(room_id)) for device_id, room_id in devices}


def effective_state(action: str | None, row: SoftwareDeviceState | None, current_sha256: str) -> str | None:
    """What to show for one computer and package.

    action: what the computer should do (None: not assigned to it). A report counts
    only if it's about the current file and the current direction (install / remove);
    otherwise the computer hasn't got to it yet: "pending". Not assigned: whatever it
    last reported (e.g. still "installed" after the assignment was deleted), or None."""
    if action is None:
        return row.state if row is not None else None
    if row is None:
        return PENDING
    if action == "install":
        if row.state in _INSTALL_STATES and row.sha256 == current_sha256:
            return row.state
        return PENDING
    return row.state if row.state in _REMOVE_STATES else PENDING
