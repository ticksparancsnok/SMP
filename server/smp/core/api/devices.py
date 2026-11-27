# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Device registry.

Devices normally appear through agent enrolment (agent module). Admins can also
pre-register devices here, e.g. to pin a known MAC/hostname to a room before the
agent is installed.
"""

import re
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, IPvAnyAddress, field_validator
from sqlalchemy import func, or_

from smp.core.api import ORMModel, Page
from smp.core.context import Ctx, require
from smp.core.models import Device, DeviceStatus, Room, TargetType
from smp.core.policy import effective_policies, emit_policy_changed, remove_assignments_for

router = APIRouter(prefix="/devices", tags=["devices"])

_MAC_RE = re.compile(r"^[0-9a-f]{12}$")


def normalise_mac(v: str | None) -> str | None:
    if v is None:
        return None
    raw = re.sub(r"[^0-9a-fA-F]", "", v).lower()
    if not _MAC_RE.match(raw):
        raise ValueError("invalid MAC address")
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2))


class DeviceCreate(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    display_name: str = ""
    mac: str | None = None
    room_id: uuid.UUID | None = None
    notes: str = ""

    v_mac = field_validator("mac")(normalise_mac)


class DeviceUpdate(BaseModel):
    display_name: str | None = None
    mac: str | None = None
    room_id: uuid.UUID | None = None
    # "fixed": room_id as set here; "network": the agent's current subnet decides
    room_mode: Literal["fixed", "network"] | None = None
    notes: str | None = None
    status: DeviceStatus | None = None

    v_mac = field_validator("mac")(normalise_mac)


class DeviceOut(ORMModel):
    id: uuid.UUID
    hostname: str
    display_name: str
    mac: str | None
    last_ip: str | None  # its own address (reported by its agent)
    observed_ip: str | None = None  # where SMP sees its agent's requests come from (differs behind a NAT)
    room_id: uuid.UUID | None
    status: str
    agent_id: str | None  # the agent key's SHA-256 fingerprint (null: no enrolled agent)
    agent_version: str | None
    os_name: str | None
    os_version: str | None
    last_seen_at: datetime | None
    room_mode: str
    logged_in_user: str | None
    notes: str


def _check_write_in(ctx: Ctx, room_id: uuid.UUID | None) -> None:
    """The caller may write devices of this room (None: a device in no room, which only
    an org-wide grant reaches). Checked before the room is looked up, so a manager
    can't tell another building's room from a missing one."""
    if room_id is None:
        ctx.check("core.devices.write")
    else:
        ctx.check("core.devices.write", room_id=room_id)


def _event(d: Device) -> dict:
    return {"id": str(d.id), "hostname": d.hostname, "room_id": str(d.room_id) if d.room_id else None}


@router.get("", response_model=Page[DeviceOut])
async def list_devices(
    ctx: Ctx = Depends(require("core.devices.read", any_room=True)),
    room_id: uuid.UUID | None = None,
    unassigned: bool = False,
    ip: IPvAnyAddress | None = None,
    q: str | None = None,
    limit: int = Query(200, le=2000),
    offset: int = 0,
):
    stmt = ctx.select(Device)
    allowed = ctx.principal.rooms_with("core.devices.read")
    if allowed is not None:
        stmt = stmt.where(Device.room_id.in_(allowed))
    if room_id:
        stmt = stmt.where(Device.room_id == room_id)
    if unassigned:
        stmt = stmt.where(Device.room_id.is_(None))
    if ip:  # its own address, or the one SMP sees (a NAT router: every PC behind it)
        stmt = stmt.where(or_(Device.last_ip == str(ip), Device.observed_ip == str(ip)))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Device.hostname.ilike(like), Device.display_name.ilike(like), Device.mac.ilike(like)))
    total = await ctx.db.scalar(stmt.with_only_columns(func.count()).order_by(None))
    rows = await ctx.db.execute(stmt.order_by(Device.hostname).limit(limit).offset(offset))
    return Page(items=list(rows.scalars()), total=total or 0)


@router.post("", response_model=DeviceOut, status_code=201)
async def create_device(body: DeviceCreate, ctx: Ctx = Depends(require("core.devices.write", any_room=True))):
    _check_write_in(ctx, body.room_id)
    if body.room_id:
        await ctx.get(Room, body.room_id)
    d = Device(org_id=ctx.org_id, status=DeviceStatus.PENDING, **body.model_dump())
    ctx.db.add(d)
    await ctx.db.flush()
    ctx.audit("core.device.create", d, body.model_dump(mode="json"))
    ctx.emit("core.device.created", _event(d))
    await ctx.commit()
    return d


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(device_id: uuid.UUID, ctx: Ctx = Depends(require("core.devices.read", any_room=True))):
    d = await ctx.get(Device, device_id)
    ctx.check("core.devices.read", d.room_id)
    return d


@router.patch("/{device_id}", response_model=DeviceOut)
async def update_device(
    device_id: uuid.UUID, body: DeviceUpdate, ctx: Ctx = Depends(require("core.devices.write", any_room=True))
):
    d = await ctx.get(Device, device_id)
    _check_write_in(ctx, d.room_id)  # where it is now
    changes = body.model_dump(exclude_unset=True)
    if "room_id" in changes and changes["room_id"] != d.room_id:
        _check_write_in(ctx, changes["room_id"])  # and where it goes
    if changes.get("room_id"):
        await ctx.get(Room, changes["room_id"])
    old_room = d.room_id
    for k, v in changes.items():
        setattr(d, k, v)
    ctx.audit("core.device.update", d, body.model_dump(mode="json", exclude_unset=True))
    ctx.emit("core.device.updated", _event(d))
    if d.room_id != old_room:  # now gets the other room's policies
        emit_policy_changed(ctx, device_id=d.id, old_room_id=old_room, room_id=d.room_id)
    await ctx.commit()
    return d


@router.delete("/{device_id}", status_code=204)
async def delete_device(device_id: uuid.UUID, ctx: Ctx = Depends(require("core.devices.write", any_room=True))):
    d = await ctx.get(Device, device_id)
    _check_write_in(ctx, d.room_id)
    removed = await remove_assignments_for(ctx, TargetType.DEVICE, d.id)
    ctx.audit("core.device.delete", d, {"hostname": d.hostname, "removed_assignments": removed})
    ctx.emit("core.device.deleted", _event(d))
    emit_policy_changed(ctx, device_id=d.id)
    await ctx.db.delete(d)
    await ctx.commit()


@router.get("/{device_id}/effective-policies")
async def device_effective_policies(
    device_id: uuid.UUID, ctx: Ctx = Depends(require("core.devices.read", any_room=True))
) -> dict[str, dict]:
    """What applies to this device right now, per policy kind, and why."""
    d = await ctx.get(Device, device_id)
    ctx.check("core.devices.read", d.room_id)
    result = await effective_policies(ctx.db, ctx.org_id, device=d)
    return {kind: ep.as_dict() for kind, ep in result.items()}
