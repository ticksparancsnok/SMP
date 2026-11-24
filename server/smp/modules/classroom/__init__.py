# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Classroom monitoring: teachers see the screens of a room's PCs.

How it uses the agent module: agent_config() tells each PC whether to send screen
frames, how often, how large and at what JPEG quality. PCs send nothing unless
someone watches their room, and full-size frames only for a PC someone enlarged
(schools often have 100 Mbps networks). Frames live in memory only (watch.py).

Settings (school defaults) and the "classroom" policy kind (per room / schedule,
e.g. a slower interval for a room on weak Wi-Fi, or monitoring only during lessons)
decide the numbers.

Screen lock: teachers lock a room's PCs (all or some) from the Monitoring page; a
locked PC shows the teacher's message with the school's logo (Settings -> Monitoring)
and blocks keyboard and mouse. Locks are stored (a server restart doesn't unlock a
room) and apply while the PC is in the room it was locked in. The agent side:
agent/smp-agent/src/capabilities/lock/. Monitoring policies don't affect locking.

Remote control: a teacher sees one PC's screen live and uses its mouse and keyboard,
through a WebSocket between browser and server; the PC sends frames and fetches the
input with signed HTTP requests (remote.py, api.py). Sessions live in memory and end
with the WebSocket. A locked PC can't be controlled, and locking ends control.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from smp.core.module import EventSubject, Module, Permission, UiRoute


class ClassroomSettings(BaseModel):
    interval_s: int = Field(5, ge=1, le=300, description="Seconds between thumbnails of a watched room")
    thumb_width: int = Field(480, ge=160, le=1280, description="Thumbnail width in pixels")
    detail_width: int = Field(1600, ge=640, le=3840, description="Width of an enlarged screen in pixels")
    detail_interval_s: int = Field(2, ge=1, le=60, description="Seconds between frames of an enlarged screen")
    jpeg_quality: int = Field(60, ge=20, le=95, description="JPEG quality (lower: less traffic)")
    remote_fps: int = Field(
        5, ge=1, le=15,
        description="Pictures per second of a remote-controlled screen (unchanged screens send nothing). "
        "5 is about 2-6 Mbit/s for that one computer",
    )
    lock_message: str = Field(
        "", max_length=500,
        description="Suggested text for locked screens (teachers can change it each time). "
        "Empty: a built-in sentence in the block page's language (DNS settings)",
    )


class ClassroomPolicy(BaseModel):
    """Per room / schedule. Empty fields use the school's settings."""

    enabled: bool = Field(True, description="Monitoring allowed (e.g. false outside lessons)")
    interval_s: int | None = Field(None, ge=1, le=300)
    thumb_width: int | None = Field(None, ge=160, le=1280)
    detail_width: int | None = Field(None, ge=640, le=3840)
    detail_interval_s: int | None = Field(None, ge=1, le=60)
    jpeg_quality: int | None = Field(None, ge=20, le=95)


LOCK_LOGO_AGENT_PATH = "/api/v1/classroom/agent/lock-logo"
REMOTE_FRAME_PATH = "/api/v1/classroom/agent/remote/frame"
REMOTE_INPUT_PATH = "/api/v1/classroom/agent/remote/input"

# Shown when neither the teacher nor the school's settings give a text. Same sentences
# as the agent's own fallback (capabilities/lock/mod.rs).
LOCK_DEFAULT_TEXT = {"hu": "A számítógépet a tanár zárolta.", "en": "This computer was locked by the teacher."}


async def lock_language(db: AsyncSession, org_id) -> str:
    """The locked screens' language: the block page's (DNS settings), so one school-wide
    setting decides what students read, not each PC's Windows language."""
    from smp.core.models import ModuleSetting
    from smp.modules.dns import DnsSettings

    row = await db.get(ModuleSetting, (org_id, "dns"))
    return DnsSettings.model_validate(row.data if row else {}).block_page_language


async def lock_default_text(db: AsyncSession, org_id) -> str:
    """What a lock without a message of its own shows: the school's suggested text, else
    the built-in sentence in the block page's language."""
    from smp.core.models import ModuleSetting

    row = await db.get(ModuleSetting, (org_id, "classroom"))
    suggested = ClassroomSettings.model_validate(row.data if row else {}).lock_message.strip()
    return suggested or LOCK_DEFAULT_TEXT[await lock_language(db, org_id)]


async def lock_config(db: AsyncSession, device) -> dict[str, Any] | None:
    """The "lock" entry of a PC's config, or None when it isn't locked (see
    agent/smp-agent/src/capabilities/lock/mod.rs for what the agent does with it).
    A few small queries, only for locked PCs; the logo itself is never in the config,
    only its fingerprint. The text is always resolved here (the teacher's, the school's,
    or the built-in one), so changing a setting changes what locked screens show."""
    from sqlalchemy import select

    from smp.modules.classroom.models import ClassroomLock, ClassroomLockLogo

    if device.room_id is None:
        return None
    message = (await db.execute(
        select(ClassroomLock.message).where(ClassroomLock.device_id == device.id, ClassroomLock.room_id == device.room_id)
    )).scalar_one_or_none()
    if message is None:
        return None
    logo = (await db.execute(
        select(ClassroomLockLogo.sha256, ClassroomLockLogo.size).where(ClassroomLockLogo.org_id == device.org_id)
    )).one_or_none()
    return {
        "message": message or await lock_default_text(db, device.org_id),
        "language": await lock_language(db, device.org_id),  # for the agent's own fallback text
        "logo": None if logo is None else {"sha256": logo.sha256, "size": logo.size, "url": LOCK_LOGO_AGENT_PATH},
    }


async def screen_params(db: AsyncSession, org_id, *, device=None, room_id=None) -> dict[str, Any]:
    """The school's settings, overridden by the effective classroom policy."""
    from smp.core.models import ModuleSetting
    from smp.core.policy import effective_policies

    row = await db.get(ModuleSetting, (org_id, "classroom"))
    params = ClassroomSettings.model_validate(row.data if row else {}).model_dump()
    effective = await effective_policies(db, org_id, device=device, room_id=room_id, kinds={"classroom"})
    policy = ClassroomPolicy.model_validate(effective["classroom"].policy.data) if "classroom" in effective else ClassroomPolicy()
    params.update({k: v for k, v in policy.model_dump().items() if v is not None and k != "enabled"})
    params["enabled"] = policy.enabled
    return params


class ClassroomModule(Module):
    name = "classroom"
    title = "Monitoring"
    description = "Screens of a room's computers for its teachers"
    permissions = (
        Permission("classroom.screens.view", "Watch the screens of a room's computers", ("teacher",), room_scoped=True),
        Permission("classroom.layout.edit", "Arrange a room's computers on the monitoring page", ("teacher",), room_scoped=True),
        Permission("classroom.screens.lock", "Lock the screens, keyboards and mice of a room's computers", ("teacher",),
                   room_scoped=True),
        Permission("classroom.screens.control", "Control a room's computers remotely (screen, mouse, keyboard)",
                   ("teacher",), room_scoped=True),
    )
    ui_routes = (UiRoute("/classroom", "Monitoring", "🖥️", "classroom.screens.view", "services"),)
    events = (
        EventSubject("classroom.watch.started", "Someone started watching a room"),
        EventSubject("classroom.watch.stopped", "Nobody watches a room any more"),
    )
    settings_model = ClassroomSettings
    policy_model = ClassroomPolicy

    def __init__(self) -> None:
        from smp.modules.classroom.remote import RemoteSessions
        from smp.modules.classroom.watch import FrameStore, WatchRegistry

        self.watches = WatchRegistry()
        self.frames = FrameStore()
        self.remote = RemoteSessions()

    def router(self) -> APIRouter:
        from smp.modules.classroom.api import router

        return router

    async def agent_config(self, db: AsyncSession, device) -> dict[str, Any] | None:
        section = await self._screen_config(db, device)
        if (lock := await lock_config(db, device)) is not None:
            section["lock"] = lock  # unlocked PCs get no "lock" key at all (older agents: same config as before)
        if (remote := await self._remote_config(db, device)) is not None:
            section["remote"] = remote  # likewise: only the one PC being controlled
        return section

    async def _remote_config(self, db: AsyncSession, device) -> dict[str, Any] | None:
        """The "remote" entry while a teacher controls this PC (see remote.py and
        agent/smp-agent/src/capabilities/remote/)."""
        session = self.remote.get(device.id)
        if session is None or session.ended.is_set():
            return None
        from smp.core.models import ModuleSetting

        row = await db.get(ModuleSetting, (device.org_id, "classroom"))
        settings = ClassroomSettings.model_validate(row.data if row else {})
        return {
            "session": session.id,
            "block_local": session.block_local,
            "interval_ms": 1000 // settings.remote_fps,
            "width": settings.detail_width,
            "quality": settings.jpeg_quality,
            "frame_path": REMOTE_FRAME_PATH,
            "input_path": REMOTE_INPUT_PATH,
        }

    async def _screen_config(self, db: AsyncSession, device) -> dict[str, Any]:
        off = {"active": False}
        if not self.watches.is_watched(device.room_id):
            return off
        p = await screen_params(db, device.org_id, device=device)
        if not p["enabled"]:
            return off
        return {
            "active": True,
            "interval_s": p["interval_s"],
            "width": p["thumb_width"],
            "quality": p["jpeg_quality"],
            "detail": device.id in self.watches.detail_devices(device.room_id),
            "detail_width": p["detail_width"],
            "detail_interval_s": p["detail_interval_s"],
            "upload_path": "/api/v1/classroom/frames",
        }


from smp.modules.classroom import models  # noqa: E402, F401  (so Alembic sees the tables)
