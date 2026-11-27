# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Software deployment: install and remove programs on the school's Windows computers.

How it fits together:
  1. A unit manager (or admin) uploads an installer (.msi or .exe, storage.py) and
     describes it as a package: silent-install arguments, how to tell it's installed,
     how to remove it, whether to restart the computer afterwards (api.py).
  2. They, or a building's manager, assign the package to the whole school (unit level
     only), a building, a room or one computer, to be installed or removed (desired.py:
     the most specific assignment wins: computer > room > building > school). A manager
     assigns only inside their own buildings and picks from the packages that exist.
  3. Each computer's agent gets its list in the heartbeat config (agent_config below,
     section "software"), downloads what it lacks from this server (signed, limited to
     a few computers at a time), installs it and reports how it went.

Why not a core policy kind (like "classroom")? Teachers with a room may assign any
existing policy to their room (exam mode), and anything that can install software
runs code as SYSTEM on the computers: teachers get no say here. What a package contains
is decided at unit level (uploads need software.packages.write, so managers can't add
their own installers); a manager only chooses which of those reach their buildings. The
installer itself still runs as SYSTEM on every computer it reaches. Policies also pick
one winner per kind, while software adds up (school baseline + room extras), and their
schedules would install and uninstall programs every lesson.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from smp.core.module import Module, Permission, UiRoute, Widget

READ = "software.status.read"
PACKAGES_WRITE = "software.packages.write"
ASSIGN = "software.assignments.write"
AGENT_DOWNLOAD_PATH = "/api/v1/software/agent/packages/{id}"
AGENT_STATUS_PATH = "/api/v1/software/agent/status"


class SoftwareSettings(BaseModel):
    max_parallel_downloads: int = Field(
        5, ge=1, le=200,
        description="Computers downloading installers at the same time; the others wait their turn "
        "(a 500 MB installer takes about a minute on 100 Mbps, so 5 at once fill such a network)",
    )
    spread_s: int = Field(
        120, ge=0, le=3600,
        description="Seconds: each computer waits a random time up to this long before it starts new installations",
    )
    reboot_warning_s: int = Field(
        300, ge=0, le=3600,
        description="Seconds the logged-in user is warned before an installation restarts the computer",
    )


async def settings_for(db: AsyncSession, org_id) -> SoftwareSettings:
    from smp.core.models import ModuleSetting

    row = await db.get(ModuleSetting, (org_id, "software"))
    return SoftwareSettings.model_validate(row.data if row else {})


def agent_entry(package, action: str) -> dict[str, Any]:
    """One package as the agent receives it (agent/.../capabilities/software, later)."""
    upload = package.upload
    return {
        "id": str(package.id),
        "name": package.name,
        "version": package.version,
        "action": action,  # "install" | "remove"
        "kind": upload.kind,  # "msi" | "exe"
        "file_name": upload.file_name,
        "size": upload.size,
        "sha256": upload.sha256,
        "url": AGENT_DOWNLOAD_PATH.format(id=package.id),
        "install_args": package.install_args,
        "uninstall_command": package.uninstall_command,
        "detection": package.detection,
        "reboot": package.reboot,  # "never" | "if_required" | "always"
        "success_codes": package.success_codes,
        "timeout_s": package.timeout_s,
    }


class SoftwareModule(Module):
    name = "software"
    title = "Software deployment"
    description = "Install and remove programs on the school's Windows computers"
    permissions = (
        Permission(READ, "See software packages and how their installation is going (room-scoped)",
                   ("teacher",), room_scoped=True),
        # Unit managers and up, school-wide: whatever a package contains runs as SYSTEM on every computer it reaches.
        Permission(PACKAGES_WRITE, "Upload, change and delete software packages", ("unit_manager",)),
        # Building-level: a manager only inside their buildings (api.py checks each target).
        Permission(ASSIGN, "Choose which computers install or remove a package", ("manager",)),
    )
    ui_routes = (UiRoute("/software", "Software deployment", "⏬", READ, "services"),)
    settings_model = SoftwareSettings

    def __init__(self) -> None:
        from smp.modules.software.storage import DownloadSlots

        self.slots = DownloadSlots()

    def router(self) -> APIRouter:
        from smp.modules.software.api import router

        return router

    async def startup(self) -> None:
        """Clear out installers nobody uses any more (also done after each upload)."""
        import logging

        from smp.core import db
        from smp.modules.software.storage import collect_garbage

        try:
            async with db.sessionmaker()() as s:
                await collect_garbage(s)
        except Exception:  # noqa: BLE001 - never keep the server from starting over clean-up
            logging.getLogger(__name__).exception("software: clean-up at startup failed")

    async def agent_config(self, db: AsyncSession, device) -> dict[str, Any] | None:
        """{"packages": [...]} for this computer, or None if nothing is assigned to it.
        Sorted, so the same state gives the same config (the agent module hashes it)."""
        from sqlalchemy import select

        from smp.modules.software.desired import desired_for_device
        from smp.modules.software.models import SoftwarePackage

        wanted = await desired_for_device(db, device.org_id, device.id, device.room_id)
        if not wanted:
            return None
        packages = (await db.execute(
            select(SoftwarePackage).where(SoftwarePackage.org_id == device.org_id, SoftwarePackage.id.in_(wanted))
        )).scalars().all()
        settings = await settings_for(db, device.org_id)
        return {
            "packages": [agent_entry(p, wanted[p.id]) for p in sorted(packages, key=lambda p: (p.name, str(p.id)))],
            "status_path": AGENT_STATUS_PATH,
            "spread_s": settings.spread_s,
            "reboot_warning_s": settings.reboot_warning_s,
        }

    async def dashboard(self, ctx) -> list[Widget]:
        if not ctx.principal.can_anywhere(READ):
            return []
        from smp.modules.software.api import overview

        failed, in_progress = await overview(ctx)
        return [Widget(
            id="software.installations", title="Failed installations", value=failed,
            hint=f"{in_progress} in progress", data={"in_progress": in_progress},
            icon="⏬", link="/software", order=70,
        )]


from smp.modules.software import models  # noqa: E402, F401  (so Alembic sees the tables)
