# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Software endpoints (mounted at /api/v1/software).

Unit managers and admins (software.packages.write, school-wide):
    POST   /uploads?filename=...      the installer as the raw request body -> an upload
    POST   /packages                  describe it: arguments, detection, removal, restart
    PATCH  /packages/{id}             change it (another upload_id = a new version)
    DELETE /packages/{id}             only while nothing is assigned
Managers (inside their buildings), unit managers and admins (software.assignments.write):
    POST   /assignments               package -> school / building / room / computer, install | remove
    PATCH  /assignments/{id}          switch between install and remove
    DELETE /assignments/{id}          (leaves the program on the computers: see desired.py)
    A manager may only touch targets inside their buildings (never the whole school).
Staff (software.status.read; teachers: their rooms' computers):
    GET    /packages                  with per-state counts over the computers you can see
    GET    /packages/{id}
    GET    /packages/{id}/assignments
    GET    /packages/{id}/devices     each computer's state
    GET    /devices/{id}              one computer: every package's state
Agents (signed requests, see modules/agent):
    GET    /agent/packages/{id}       the installer, only for computers that should have
                                      it; Range for resuming; 503 + Retry-After while all
                                      download slots are taken
    POST   /agent/status              what happened (installed, failed, ...)
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import AfterValidator, BaseModel, Field, TypeAdapter, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from smp.core import db
from smp.core.context import Ctx, require
from smp.core.dashboard import ONLINE_WINDOW
from smp.core.errors import AppError
from smp.core.models import Building, Device, DeviceStatus, Room
from smp.modules.agent.api import authenticate_agent
from smp.modules.software import ASSIGN, PACKAGES_WRITE, READ, settings_for, storage
from smp.modules.software.desired import (
    FAILED,
    IN_PROGRESS,
    REPORTED_STATES,
    desired_for_device,
    desired_for_devices,
    effective_state,
)
from smp.modules.software.models import SoftwareAssignment, SoftwareDeviceState, SoftwarePackage, SoftwareUpload

router = APIRouter()
log = logging.getLogger(__name__)

READ_CHUNK = 256 * 1024


def _mod(request: Request):
    return request.app.state.registry.get("software")


def _wake(ctx: Ctx, reason: str) -> None:
    """Computers' package lists changed: waiting heartbeats answer at once (agent module)."""
    ctx.emit("agent.config.changed", {"reason": reason})


# --------------------------------------------------------------------------- schemas

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _one_line(v: str | None) -> str | None:
    if v is not None and _CONTROL.search(v):
        raise ValueError("must be a single line without control characters")
    return v


OneLine = Annotated[str, AfterValidator(_one_line)]
# Passed to the installer as its command line (no shell is involved on the computer).
CommandLine = Annotated[str, Field(max_length=2000), AfterValidator(_one_line)]
GUID_RE = r"^\{[0-9A-Fa-f]{8}-([0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\}$"


class MsiDetection(BaseModel):
    """Installed = Windows Installer knows the product code. Without one, the agent
    reads it from the .msi file itself. Only for .msi packages."""

    type: Literal["msi"]
    product_code: str | None = Field(None, pattern=GUID_RE)


class RegistryDetection(BaseModel):
    """Installed = this key exists under HKEY_LOCAL_MACHINE (the agent looks in the 64-
    and the 32-bit view). With `value`: that value exists in it; with `equals` too: its
    data is exactly that (e.g. DisplayVersion = 24.08, so a new version reinstalls)."""

    type: Literal["registry"]
    key: OneLine = Field(min_length=6, max_length=500)  # e.g. HKLM\SOFTWARE\...\Uninstall\7-Zip
    value: OneLine | None = Field(None, max_length=255)
    equals: OneLine | None = Field(None, max_length=255)

    @field_validator("key")
    @classmethod
    def _hklm(cls, v: str) -> str:
        if not v.upper().startswith(("HKLM\\", "HKEY_LOCAL_MACHINE\\")):
            raise ValueError("must be a key under HKLM\\ (the agent runs as SYSTEM: no HKCU)")
        return v

    @model_validator(mode="after")
    def _equals_needs_value(self):
        if self.equals is not None and self.value is None:
            raise ValueError("'equals' needs 'value' (which value's data to compare)")
        return self


class FileDetection(BaseModel):
    """Installed = this file exists. %ProgramFiles%, %ProgramFiles(x86)%, %SystemRoot%
    and the like are expanded on the computer."""

    type: Literal["file"]
    path: OneLine = Field(min_length=4, max_length=500, pattern=r"^([A-Za-z]:\\|%[A-Za-z0-9_()]+%\\)")


Detection = Annotated[MsiDetection | RegistryDetection | FileDetection, Field(discriminator="type")]
_detection = TypeAdapter(Detection)
Reboot = Literal["never", "if_required", "always"]
# Windows exit codes are 32-bit; tools show them signed or unsigned.
ExitCode = Annotated[int, Field(ge=-(2**31), le=2**32 - 1)]


class SoftwarePackageIn(BaseModel):
    upload_id: uuid.UUID
    name: OneLine = Field(min_length=1, max_length=200)
    version: OneLine = Field("", max_length=64)
    publisher: OneLine = Field("", max_length=200)
    notes: str = Field("", max_length=5000)
    install_args: CommandLine = ""
    uninstall_command: CommandLine = ""
    # None: .msi packages are recognised by their product code; .exe packages need a rule.
    detection: Detection | None = None
    reboot: Reboot = "never"
    success_codes: list[ExitCode] = Field(default_factory=list, max_length=20)
    timeout_s: int = Field(3600, ge=60, le=6 * 3600)


class SoftwarePackageUpdate(BaseModel):
    upload_id: uuid.UUID | None = None
    name: OneLine | None = Field(None, min_length=1, max_length=200)
    version: OneLine | None = Field(None, max_length=64)
    publisher: OneLine | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=5000)
    install_args: CommandLine | None = None
    uninstall_command: CommandLine | None = None
    detection: Detection | None = None  # explicit null: back to the .msi default
    reboot: Reboot | None = None
    success_codes: list[ExitCode] | None = Field(None, max_length=20)
    timeout_s: int | None = Field(None, ge=60, le=6 * 3600)


class SoftwareUploadOut(BaseModel):
    id: uuid.UUID
    file_name: str
    size: int
    sha256: str
    kind: str
    created_at: datetime


class SoftwarePackageOut(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    publisher: str
    notes: str
    upload_id: uuid.UUID
    kind: str
    file_name: str
    size: int
    sha256: str
    install_args: str
    uninstall_command: str
    detection: dict[str, Any]
    reboot: str
    success_codes: list[int]
    timeout_s: int
    removable: bool  # SMP can uninstall it (.msi, or an uninstall command is set)
    created_at: datetime
    updated_at: datetime
    assignments: int = 0
    # Computers per state ("installed", "pending", "failed", ...), of those the caller sees.
    counts: dict[str, int] = {}


class SoftwareAssignmentIn(BaseModel):
    package_id: uuid.UUID
    target_type: Literal["org", "building", "room", "device"]
    target_id: uuid.UUID | None = None
    action: Literal["install", "remove"] = "install"

    @model_validator(mode="after")
    def _target(self):
        if (self.target_type == "org") != (self.target_id is None):
            raise ValueError("target_id is required unless target_type is 'org' (and must be empty then)")
        return self


class SoftwareAssignmentUpdate(BaseModel):
    action: Literal["install", "remove"]


class SoftwareAssignmentOut(BaseModel):
    id: uuid.UUID
    package_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID | None
    target_name: str | None  # building or room name, or hostname; None for the whole school
    action: str
    created_at: datetime


class SoftwareDeviceStateOut(BaseModel):
    device_id: uuid.UUID
    hostname: str
    room_id: uuid.UUID | None
    room_name: str | None
    online: bool
    action: str | None  # what it should do; None: not assigned (any more)
    state: str | None  # effective: "pending" until it reports on the current file
    exit_code: int | None = None
    message: str = ""
    updated_at: datetime | None = None


class SoftwarePackageStateOut(BaseModel):
    package_id: uuid.UUID
    name: str
    version: str
    action: str | None
    state: str | None
    exit_code: int | None = None
    message: str = ""
    updated_at: datetime | None = None


# --------------------------------------------------------------------------- helpers


def removable(kind: str, uninstall_command: str) -> bool:
    return kind == "msi" or bool(uninstall_command.strip())


def _settle_detection(kind: str, detection: Any) -> dict[str, Any]:
    """The detection rule to store, checked against the file kind."""
    if detection is None:
        if kind != "msi":
            raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "software.detection_required",
                           "a program (.exe) needs a detection rule: a registry key or a file its installation creates")
        return {"type": "msi", "product_code": None}
    if detection.type == "msi" and kind != "msi":
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "software.msi_detection_needs_msi",
                       "a product code rule only works for .msi packages")
    return detection.model_dump()


def _out(p: SoftwarePackage, counts: Counter | None = None, assignments: int = 0) -> SoftwarePackageOut:
    u = p.upload
    return SoftwarePackageOut(
        id=p.id, name=p.name, version=p.version, publisher=p.publisher, notes=p.notes, upload_id=u.id,
        kind=u.kind, file_name=u.file_name, size=u.size, sha256=u.sha256, install_args=p.install_args,
        uninstall_command=p.uninstall_command, detection=p.detection, reboot=p.reboot,
        success_codes=p.success_codes, timeout_s=p.timeout_s, removable=removable(u.kind, p.uninstall_command),
        created_at=p.created_at, updated_at=p.updated_at, assignments=assignments, counts=dict(counts or {}),
    )


async def _visible_devices(ctx: Ctx) -> list[Device]:
    """Computers with an agent the caller may see (teachers: their rooms)."""
    stmt = ctx.select(Device).where(Device.agent_id.is_not(None), Device.status != DeviceStatus.RETIRED)
    if (rooms := ctx.principal.rooms_with(READ)) is not None:
        stmt = stmt.where(Device.room_id.in_(rooms))
    return list((await ctx.db.execute(stmt.order_by(Device.hostname))).scalars())


async def _states(ctx: Ctx, device_ids: list[uuid.UUID], package_id: uuid.UUID | None = None):
    stmt = ctx.select(SoftwareDeviceState).where(SoftwareDeviceState.device_id.in_(device_ids))
    if package_id is not None:
        stmt = stmt.where(SoftwareDeviceState.package_id == package_id)
    return {(r.device_id, r.package_id): r for r in (await ctx.db.execute(stmt)).scalars()}


def _tally(current: dict[uuid.UUID, str], devices: list[Device], desired, states) -> dict[uuid.UUID, Counter]:
    """Per package: how many of `devices` are in which effective state.
    `current`: package id -> SHA-256 of its current file."""
    counts: dict[uuid.UUID, Counter] = defaultdict(Counter)
    for d in devices:
        for pkg_id, action in desired.get(d.id, {}).items():
            if pkg_id in current:
                counts[pkg_id][effective_state(action, states.get((d.id, pkg_id)), current[pkg_id])] += 1
    for (device_id, pkg_id), row in states.items():
        if pkg_id in current and pkg_id not in desired.get(device_id, {}):
            counts[pkg_id][row.state] += 1  # not assigned any more: what it last said
    return counts


async def _room_names(ctx: Ctx) -> dict[uuid.UUID, str]:
    return dict((await ctx.db.execute(select(Room.id, Room.name).where(Room.org_id == ctx.org_id))).all())


def _online(d: Device) -> bool:
    return d.last_seen_at is not None and d.last_seen_at >= datetime.now(UTC) - ONLINE_WINDOW


async def overview(ctx: Ctx) -> tuple[int, int]:
    """(failed, in progress) installations on the computers the caller can see: the
    home-page card. Plain counts of the last reports (cheap: it refreshes every 10 s)."""
    stmt = (
        select(SoftwareDeviceState.state, func.count())
        .join(Device, Device.id == SoftwareDeviceState.device_id)
        .where(SoftwareDeviceState.org_id == ctx.org_id, Device.status != DeviceStatus.RETIRED,
               SoftwareDeviceState.state.in_(FAILED | IN_PROGRESS))
        .group_by(SoftwareDeviceState.state)
    )
    if (rooms := ctx.principal.rooms_with(READ)) is not None:
        stmt = stmt.where(Device.room_id.in_(rooms))
    by_state = dict((await ctx.db.execute(stmt)).all())
    return sum(by_state.get(s, 0) for s in FAILED), sum(by_state.get(s, 0) for s in IN_PROGRESS)


# --------------------------------------------------------------------------- uploads and packages


@router.post("/uploads", response_model=SoftwareUploadOut, status_code=201)
async def upload(
    request: Request,
    filename: str = Query(min_length=1, max_length=255, description="the installer's file name (.msi or .exe)"),
    ctx: Ctx = Depends(require(PACKAGES_WRITE)),
):
    """The installer file as the raw request body (not a form). Stored once per content."""
    # Don't keep a database connection (and transaction) open while gigabytes arrive.
    await ctx.db.rollback()
    got = await storage.receive(request, filename)
    u = SoftwareUpload(org_id=ctx.org_id, sha256=got.sha256, size=got.size, kind=got.kind,
                       file_name=got.file_name, created_by=ctx.principal.id)
    ctx.db.add(u)
    await ctx.db.flush()
    ctx.audit("software.upload.create", u, {"file_name": u.file_name, "size": u.size, "sha256": u.sha256, "kind": u.kind})
    await ctx.commit()
    try:
        await storage.collect_garbage(ctx.db)
    except Exception:  # noqa: BLE001 - the upload itself worked
        log.exception("software: clean-up after an upload failed")
    return SoftwareUploadOut(id=u.id, file_name=u.file_name, size=u.size, sha256=u.sha256, kind=u.kind, created_at=u.created_at)


@router.get("/packages", response_model=list[SoftwarePackageOut])
async def list_packages(ctx: Ctx = Depends(require(READ, any_room=True))):
    packages = list((await ctx.db.execute(ctx.select(SoftwarePackage).order_by(SoftwarePackage.name))).scalars())
    if not packages:
        return []
    devices = await _visible_devices(ctx)
    desired = await desired_for_devices(ctx.db, ctx.org_id, [(d.id, d.room_id) for d in devices])
    states = await _states(ctx, [d.id for d in devices])
    counts = _tally({p.id: p.upload.sha256 for p in packages}, devices, desired, states)
    assigned = dict((await ctx.db.execute(
        select(SoftwareAssignment.package_id, func.count())
        .where(SoftwareAssignment.org_id == ctx.org_id).group_by(SoftwareAssignment.package_id)
    )).all())
    return [_out(p, counts.get(p.id), assigned.get(p.id, 0)) for p in packages]


@router.post("/packages", response_model=SoftwarePackageOut, status_code=201)
async def create_package(body: SoftwarePackageIn, ctx: Ctx = Depends(require(PACKAGES_WRITE))):
    upload_row = await ctx.get(SoftwareUpload, body.upload_id)
    detection = _settle_detection(upload_row.kind, body.detection)
    fields = body.model_dump(exclude={"upload_id", "detection"})
    p = SoftwarePackage(org_id=ctx.org_id, upload=upload_row, detection=detection, created_by=ctx.principal.id, **fields)
    ctx.db.add(p)
    await ctx.db.flush()
    ctx.audit("software.package.create", p, {
        **fields, "detection": detection, "file_name": upload_row.file_name, "sha256": upload_row.sha256,
    })
    await ctx.commit()
    return _out(p)


@router.get("/packages/{package_id}", response_model=SoftwarePackageOut)
async def get_package(package_id: uuid.UUID, ctx: Ctx = Depends(require(READ, any_room=True))):
    p = await ctx.get(SoftwarePackage, package_id)
    devices = await _visible_devices(ctx)
    desired = await desired_for_devices(ctx.db, ctx.org_id, [(d.id, d.room_id) for d in devices], package_id=p.id)
    states = await _states(ctx, [d.id for d in devices], package_id=p.id)
    counts = _tally({p.id: p.upload.sha256}, devices, desired, states)
    n = await ctx.db.scalar(select(func.count()).where(SoftwareAssignment.package_id == p.id))
    return _out(p, counts.get(p.id), n or 0)


@router.patch("/packages/{package_id}", response_model=SoftwarePackageOut)
async def update_package(package_id: uuid.UUID, body: SoftwarePackageUpdate, ctx: Ctx = Depends(require(PACKAGES_WRITE))):
    p = await ctx.get(SoftwarePackage, package_id)
    changes = body.model_dump(exclude_unset=True, mode="json")
    for required in ("upload_id", "name", "version", "publisher", "notes", "install_args", "uninstall_command",
                     "reboot", "success_codes", "timeout_s"):
        if required in changes and changes[required] is None:
            raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "software.field_required",
                           f"{required} can't be empty", field=required)
    old_sha = p.upload.sha256
    new_upload = await ctx.get(SoftwareUpload, body.upload_id) if body.upload_id else p.upload
    detection_in = body.detection if "detection" in changes else _detection.validate_python(p.detection)
    detection = _settle_detection(new_upload.kind, detection_in)
    uninstall = body.uninstall_command if body.uninstall_command is not None else p.uninstall_command
    if not removable(new_upload.kind, uninstall):
        removals = await ctx.db.scalar(select(func.count()).where(
            SoftwareAssignment.package_id == p.id, SoftwareAssignment.action == "remove"))
        if removals:
            raise AppError(status.HTTP_409_CONFLICT, "software.not_removable",
                           "computers are told to remove this package: it needs an uninstall command")

    for field in ("name", "version", "publisher", "notes", "install_args", "uninstall_command", "reboot",
                  "success_codes", "timeout_s"):
        if field in changes:
            setattr(p, field, getattr(body, field))
    p.upload = new_upload
    p.detection = detection
    p.updated_at = datetime.now(UTC)  # also when only the file changed
    details = {**changes, "detection": detection}
    if new_upload.sha256 != old_sha:  # a new version: computers that have it install it again
        details.update(file_name=new_upload.file_name, sha256=new_upload.sha256, previous_sha256=old_sha)
    ctx.audit("software.package.update", p, details)
    _wake(ctx, "software package changed")
    await ctx.commit()
    return await get_package(package_id, ctx)


@router.delete("/packages/{package_id}", status_code=204)
async def delete_package(package_id: uuid.UUID, ctx: Ctx = Depends(require(PACKAGES_WRITE))):
    p = await ctx.get(SoftwarePackage, package_id)
    n = await ctx.db.scalar(select(func.count()).where(SoftwareAssignment.package_id == p.id))
    if n:
        # Removing it with assignments would leave computers with a program SMP can no
        # longer remove or update; make the admin decide what happens to them first.
        raise AppError(status.HTTP_409_CONFLICT, "software.package_in_use",
                       f"the package has {n} assignment(s); delete those first", count=n)
    ctx.audit("software.package.delete", p, {"name": p.name, "version": p.version, "sha256": p.upload.sha256})
    await ctx.db.delete(p)
    await ctx.commit()  # its file goes with a later clean-up (storage.collect_garbage)


# --------------------------------------------------------------------------- assignments


@dataclass(frozen=True)
class _Target:
    """An assignment's target, checked to be this school's."""

    type: str
    building_id: uuid.UUID | None = None
    room_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    name: str | None = None
    # Where the target sits (a computer: its room), for the scope check and the audit.
    scope_room_id: uuid.UUID | None = None
    scope_building_id: uuid.UUID | None = None

    @property
    def id(self) -> uuid.UUID | None:
        return self.building_id or self.room_id or self.device_id

    @property
    def audit_buildings(self) -> list[uuid.UUID] | None:
        """Which buildings an audit entry about it concerns (None: Ctx.audit works it out)."""
        return [self.scope_building_id] if self.scope_building_id is not None else None


async def _target(ctx: Ctx, target_type: str, target_id: uuid.UUID | None) -> _Target:
    """The target of an assignment, checking it is this school's."""
    if target_type == "building":
        building = await ctx.get(Building, target_id)
        return _Target(target_type, building_id=building.id, name=building.name, scope_building_id=building.id)
    if target_type == "room":
        room = await ctx.get(Room, target_id)
        return _Target(target_type, room_id=room.id, name=room.name, scope_room_id=room.id,
                       scope_building_id=room.building_id)
    if target_type == "device":
        device = await ctx.get(Device, target_id)
        room = await ctx.db.get(Room, device.room_id) if device.room_id is not None else None
        return _Target(target_type, device_id=device.id, name=device.hostname, scope_room_id=device.room_id,
                       scope_building_id=room.building_id if room is not None else None)
    return _Target(target_type)


def _check_assign(ctx: Ctx, t: _Target) -> None:
    """The caller may assign to this target: the whole school needs the school-wide
    permission; a building, room or computer (through its room) the one for it. A
    computer in no room counts as school-level."""
    if t.type == "building":
        ctx.check(ASSIGN, building_id=t.building_id)
    elif t.type in ("room", "device"):
        ctx.check(ASSIGN, room_id=t.scope_room_id)
    else:
        ctx.check(ASSIGN)


def _out_assignment(a: SoftwareAssignment, name: str | None) -> SoftwareAssignmentOut:
    return SoftwareAssignmentOut(
        id=a.id, package_id=a.package_id, target_type=a.target_type,
        target_id=a.building_id or a.room_id or a.device_id, target_name=name, action=a.action, created_at=a.created_at,
    )


@router.get("/packages/{package_id}/assignments", response_model=list[SoftwareAssignmentOut])
async def list_assignments(package_id: uuid.UUID, ctx: Ctx = Depends(require(READ, any_room=True))):
    """The school-wide ones (they reach the caller's computers too) and those for the
    caller's buildings, rooms and computers; teachers and managers see no others."""
    await ctx.get(SoftwarePackage, package_id)
    rows = list((await ctx.db.execute(
        ctx.select(SoftwareAssignment).where(SoftwareAssignment.package_id == package_id)
        .order_by(SoftwareAssignment.created_at)
    )).scalars())
    rooms = await _room_names(ctx)
    buildings = dict((await ctx.db.execute(select(Building.id, Building.name).where(Building.org_id == ctx.org_id))).all())
    device_ids = [a.device_id for a in rows if a.device_id]
    devices = {d.id: d for d in (await ctx.db.execute(ctx.select(Device).where(Device.id.in_(device_ids)))).scalars()}
    allowed = ctx.principal.rooms_with(READ)
    visible_buildings: set[uuid.UUID] = set()
    if allowed:  # a building counts as visible when one of its rooms is
        visible_buildings = set((await ctx.db.execute(
            select(Room.building_id).where(Room.org_id == ctx.org_id, Room.id.in_(allowed), Room.building_id.is_not(None))
        )).scalars())
    out = []
    for a in rows:
        if allowed is not None and a.target_type != "org":
            if a.target_type == "building":
                if a.building_id not in visible_buildings:
                    continue
            else:
                room = a.room_id if a.target_type == "room" else (
                    devices[a.device_id].room_id if a.device_id in devices else None)
                if room not in allowed:
                    continue
        if a.building_id:
            name = buildings.get(a.building_id)
        elif a.room_id:
            name = rooms.get(a.room_id)
        else:
            name = devices[a.device_id].hostname if a.device_id in devices else None
        out.append(_out_assignment(a, name))
    return out


@router.post("/assignments", response_model=SoftwareAssignmentOut, status_code=201)
async def create_assignment(body: SoftwareAssignmentIn, ctx: Ctx = Depends(require(ASSIGN, any_room=True))):
    p = await ctx.get(SoftwarePackage, body.package_id)
    t = await _target(ctx, body.target_type, body.target_id)
    _check_assign(ctx, t)  # managers: only inside their buildings
    if body.action == "remove" and not removable(p.upload.kind, p.uninstall_command):
        raise AppError(status.HTTP_409_CONFLICT, "software.not_removable",
                       "this package can't be removed by SMP: give it an uninstall command first")
    exists_already = await ctx.db.scalar(select(func.count()).where(
        SoftwareAssignment.package_id == p.id,
        SoftwareAssignment.building_id.is_(None) if t.building_id is None else SoftwareAssignment.building_id == t.building_id,
        SoftwareAssignment.room_id.is_(None) if t.room_id is None else SoftwareAssignment.room_id == t.room_id,
        SoftwareAssignment.device_id.is_(None) if t.device_id is None else SoftwareAssignment.device_id == t.device_id,
    ))
    if exists_already:
        raise AppError(status.HTTP_409_CONFLICT, "software.assignment_exists",
                       "this package is already assigned there; change that assignment instead")
    a = SoftwareAssignment(org_id=ctx.org_id, package_id=p.id, target_type=body.target_type,
                           building_id=t.building_id, room_id=t.room_id, device_id=t.device_id,
                           action=body.action, created_by=ctx.principal.id)
    ctx.db.add(a)
    await ctx.db.flush()
    ctx.audit("software.assignment.create", a, {
        "package_id": str(p.id), "package": p.name, "version": p.version, "target_type": a.target_type,
        "target_id": str(t.id) if t.id else None, "target_name": t.name, "action": a.action,
    }, building_ids=t.audit_buildings)
    _wake(ctx, "software assigned")
    await ctx.commit()
    return _out_assignment(a, t.name)


@router.patch("/assignments/{assignment_id}", response_model=SoftwareAssignmentOut)
async def update_assignment(assignment_id: uuid.UUID, body: SoftwareAssignmentUpdate,
                            ctx: Ctx = Depends(require(ASSIGN, any_room=True))):
    a = await ctx.get(SoftwareAssignment, assignment_id)
    t = await _target(ctx, a.target_type, a.building_id or a.room_id or a.device_id)
    _check_assign(ctx, t)  # only the action changes, the target stays
    p = await ctx.get(SoftwarePackage, a.package_id)
    if body.action == "remove" and not removable(p.upload.kind, p.uninstall_command):
        raise AppError(status.HTTP_409_CONFLICT, "software.not_removable",
                       "this package can't be removed by SMP: give it an uninstall command first")
    if a.action != body.action:
        ctx.audit("software.assignment.update", a, {
            "package": p.name, "target_type": a.target_type, "target_name": t.name, "action": body.action,
            "previous_action": a.action,
        }, building_ids=t.audit_buildings)
        a.action = body.action
        _wake(ctx, "software assignment changed")
        await ctx.commit()
    return _out_assignment(a, t.name)


@router.delete("/assignments/{assignment_id}", status_code=204)
async def delete_assignment(assignment_id: uuid.UUID, ctx: Ctx = Depends(require(ASSIGN, any_room=True))):
    """The program stays where it is installed (assign "remove" to uninstall it)."""
    a = await ctx.get(SoftwareAssignment, assignment_id)
    t = await _target(ctx, a.target_type, a.building_id or a.room_id or a.device_id)
    _check_assign(ctx, t)
    p = await ctx.get(SoftwarePackage, a.package_id)
    ctx.audit("software.assignment.delete", a, {
        "package": p.name, "target_type": a.target_type, "target_id": str(t.id) if t.id else None,
        "target_name": t.name, "action": a.action,
    }, building_ids=t.audit_buildings)
    await ctx.db.delete(a)
    _wake(ctx, "software unassigned")
    await ctx.commit()


# --------------------------------------------------------------------------- status


@router.get("/packages/{package_id}/devices", response_model=list[SoftwareDeviceStateOut])
async def package_devices(package_id: uuid.UUID, ctx: Ctx = Depends(require(READ, any_room=True))):
    """Every computer (that the caller can see) this package is assigned to or that has
    reported about it, by hostname."""
    p = await ctx.get(SoftwarePackage, package_id)
    devices = await _visible_devices(ctx)
    desired = await desired_for_devices(ctx.db, ctx.org_id, [(d.id, d.room_id) for d in devices], package_id=p.id)
    states = await _states(ctx, [d.id for d in devices], package_id=p.id)
    rooms = await _room_names(ctx)
    out = []
    for d in devices:
        action, row = desired.get(d.id, {}).get(p.id), states.get((d.id, p.id))
        if action is None and row is None:
            continue
        out.append(SoftwareDeviceStateOut(
            device_id=d.id, hostname=d.hostname, room_id=d.room_id, room_name=rooms.get(d.room_id),
            online=_online(d), action=action, state=effective_state(action, row, p.upload.sha256),
            exit_code=row.exit_code if row else None, message=row.message if row else "",
            updated_at=row.updated_at if row else None,
        ))
    return out


@router.get("/devices/{device_id}", response_model=list[SoftwarePackageStateOut])
async def device_packages(device_id: uuid.UUID, ctx: Ctx = Depends(require(READ, any_room=True))):
    """One computer: the packages assigned to it or that it reported about."""
    d = await ctx.get(Device, device_id)
    ctx.check(READ, d.room_id)
    desired = await desired_for_device(ctx.db, ctx.org_id, d.id, d.room_id)
    states = {pkg: row for (_, pkg), row in (await _states(ctx, [d.id])).items()}
    ids = set(desired) | set(states)
    packages = (await ctx.db.execute(ctx.select(SoftwarePackage).where(SoftwarePackage.id.in_(ids)))).scalars()
    out = []
    for p in sorted(packages, key=lambda p: (p.name.lower(), p.version)):
        action, row = desired.get(p.id), states.get(p.id)
        out.append(SoftwarePackageStateOut(
            package_id=p.id, name=p.name, version=p.version, action=action,
            state=effective_state(action, row, p.upload.sha256), exit_code=row.exit_code if row else None,
            message=row.message if row else "", updated_at=row.updated_at if row else None,
        ))
    return out


# --------------------------------------------------------------------------- agents


async def _stream(path: Path, start: int, length: int, lease: storage.Lease):
    """The file's bytes [start, start + length); frees the download slot however it ends
    (finished, or the computer went away mid-download)."""
    try:
        f = await run_in_threadpool(open, path, "rb")
        try:
            await run_in_threadpool(f.seek, start)
            remaining = length
            while remaining > 0:
                chunk = await run_in_threadpool(f.read, min(READ_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            f.close()
    finally:
        lease.release()


@router.get("/agent/packages/{package_id}", include_in_schema=False)
async def agent_download(package_id: uuid.UUID, request: Request):
    """An installer, for a computer that should install (or remove) it. Not public:
    installers can carry license keys."""
    device, _ = await authenticate_agent(request)
    async with db.sessionmaker()() as s:
        p = await s.get(SoftwarePackage, package_id)
        wanted = await desired_for_device(s, device.org_id, device.id, device.room_id) if p is not None else {}
        if p is None or p.org_id != device.org_id or package_id not in wanted:
            raise AppError(status.HTTP_404_NOT_FOUND, "software.not_assigned", "this package isn't assigned to this computer")
        sha256, size = p.upload.sha256, p.upload.size
        settings = await settings_for(s, device.org_id)
    path = storage.file_path(sha256)
    if not await run_in_threadpool(path.is_file):
        log.error("software: the file of package %s (%s) is missing from %s", package_id, sha256, path.parent)
        raise AppError(status.HTTP_404_NOT_FOUND, "software.file_missing", "the installer file is missing on the server")
    try:
        wanted_range = storage.parse_range(request.headers.get("range"), size)
    except storage.RangeNotSatisfiable:
        raise AppError(status.HTTP_416_RANGE_NOT_SATISFIABLE, "software.bad_range", "range not satisfiable",
                       headers={"Content-Range": f"bytes */{size}"}) from None
    lease = _mod(request).slots.try_acquire(settings.max_parallel_downloads)
    if lease is None:
        # Spread the retries a little, so the waiting computers don't all come back at once.
        raise AppError(status.HTTP_503_SERVICE_UNAVAILABLE, "software.busy",
                       "other computers are downloading right now; try again later",
                       headers={"Retry-After": str(20 + secrets.randbelow(41))})
    start, end = wanted_range or (0, size - 1)
    headers = {
        "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store",
        "X-Checksum-SHA256": sha256,
    }
    if wanted_range is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(_stream(path, start, end - start + 1, lease), media_type="application/octet-stream",
                             status_code=206 if wanted_range is not None else 200, headers=headers)


class Report(BaseModel):
    package_id: uuid.UUID
    state: str
    sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")  # the file this is about
    exit_code: ExitCode | None = None
    message: str = Field("", max_length=500)

    @field_validator("state")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in REPORTED_STATES:
            raise ValueError(f"unknown state {v!r}")
        return v


class StatusIn(BaseModel):
    reports: list[Report] = Field(max_length=200)


@router.post("/agent/status", status_code=204, include_in_schema=False)
async def agent_status(request: Request):
    """What a computer did with its packages. Reports about packages that aren't its
    school's are ignored; the newest report per package wins."""
    device, raw = await authenticate_agent(request)
    try:
        body = StatusIn.model_validate_json(raw or b"{}")
    except ValidationError as e:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "software.bad_report",
                       f"invalid body: {e.error_count()} error(s)") from e
    latest = {r.package_id: r for r in body.reports}
    if not latest:
        return
    now = datetime.now(UTC)
    async with db.sessionmaker()() as s:
        known = set((await s.execute(select(SoftwarePackage.id).where(
            SoftwarePackage.org_id == device.org_id, SoftwarePackage.id.in_(latest)))).scalars())
        rows = [
            {"org_id": device.org_id, "device_id": device.id, "package_id": r.package_id, "state": r.state,
             "sha256": r.sha256, "exit_code": r.exit_code, "message": _CONTROL.sub(" ", r.message), "updated_at": now}
            for r in latest.values() if r.package_id in known
        ]
        if rows:
            stmt = insert(SoftwareDeviceState).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["device_id", "package_id"],
                set_={c: stmt.excluded[c] for c in ("state", "sha256", "exit_code", "message", "updated_at")},
            )
            await s.execute(stmt)
            await s.commit()
