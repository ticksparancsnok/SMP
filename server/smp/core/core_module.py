# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)  # ruff: ignore[missing-copyright-notice]
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""The core registers itself as a module so its permissions, routes and events go through exactly the same machinery as everyone else's."""  # ruff: ignore[line-too-long]

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from smp.core.api import (
    auth,
    buildings,
    devices,
    groups,
    platform,
    policies,
    rooms,
    users,
)
from smp.core.context import Ctx
from smp.core.dashboard import core_widgets
from smp.core.module import EventSubject, Module, Permission, UiRoute, Widget

ADMIN = ()  # admin implicitly has every permission
# Each role also gets the defaults of the roles it inherits (smp/core/roles.py):
# manager = teacher + MANAGER; unit_manager = manager + UNIT_MANAGER.
TEACHER = ("teacher",)
MANAGER = ("manager",)
UNIT_MANAGER = ("unit_manager",)


class CoreSettings(BaseModel):  # ruff: ignore[undocumented-public-class]
    school_name: str = ""
    audit_retention_days: int = Field(365, ge=30, le=3650)
    otp_valid_days: int = Field(
        14, ge=1, le=180, description="One-time (initial) passwords are valid for this many days; after that a new one is needed."  # ruff: ignore[line-too-long]
    )


class CoreModule(Module):  # ruff: ignore[undocumented-public-class]
    name = "core"
    title = "Core"
    description = "Users, groups, rooms, devices, policies, audit"

    permissions = (
        Permission("core.users.read", "View users", TEACHER),
        Permission("core.users.write", "Create, edit, delete users and assign roles", MANAGER),
        Permission(
            "core.users.reset_class_password",
            "Issue a new one-time password to students of groups the user teaches",  # ruff: ignore[line-too-long]
            TEACHER,
            org_wide=True,  # checked by the group relation, not by place
        ),
        Permission("core.groups.read", "View groups and members", TEACHER),
        Permission("core.groups.write", "Manage groups and membership", MANAGER),
        Permission("core.rooms.read", "View rooms (room-scoped)", TEACHER, room_scoped=True),
        Permission("core.buildings.read", "View buildings", TEACHER),
        Permission("core.buildings.write", "Create, rename and delete buildings", UNIT_MANAGER),
        Permission("core.rooms.write", "Manage rooms and their subnets", MANAGER),
        Permission("core.devices.read", "View devices (room-scoped)", TEACHER, room_scoped=True),
        Permission("core.devices.write", "Register, move and retire devices", MANAGER),
        Permission("core.policies.read", "View policies, schedules and assignments", TEACHER),
        Permission("core.policies.write", "Create and edit policies and schedules", MANAGER),
        Permission("core.policies.assign", "Apply policies to targets (room-scoped for teachers)", TEACHER, room_scoped=True),
        Permission("core.audit.read", "Read the audit log", MANAGER),
        Permission("core.settings.read", "Read module settings", ADMIN),
        Permission("core.settings.write", "Change module settings", ADMIN),
        Permission("core.tokens.manage", "Create and revoke API tokens", UNIT_MANAGER),
    )

    ui_routes = (
        # Titles are English; the web UI translates them by path.
        UiRoute("/", "Home", "🏠", None, "overview"),
        UiRoute("/account", "My account", "👤", None, "account"),
        UiRoute("/users", "Users", "🎓", "core.users.read", "admin"),
        UiRoute("/groups", "Groups", "👥", "core.groups.read", "admin"),
        UiRoute("/buildings", "Buildings", "🏫", "core.buildings.read", "admin"),
        UiRoute("/rooms", "Rooms", "🏢", "core.rooms.read", "admin"),
        UiRoute("/devices", "Devices", "💻", "core.devices.read", "admin"),
        UiRoute("/policies", "Policies", "📋", "core.policies.read", "admin"),
        UiRoute("/audit", "Audit log", "📜", "core.audit.read", "admin"),
        UiRoute("/settings", "Settings", "⚙️", "core.settings.read", "admin"),
    )

    events = (
        EventSubject("core.user.created", "User created"),
        EventSubject("core.user.updated", "User profile or roles changed"),
        EventSubject("core.user.deleted", "User deleted"),
        EventSubject("core.group.created", "Group created"),
        EventSubject("core.group.updated", "Group renamed/edited"),
        EventSubject("core.group.deleted", "Group deleted"),
        EventSubject("core.group.members_changed", "Group membership replaced"),
        EventSubject("core.group.teachers_changed", "Group class teachers replaced"),
        EventSubject("core.building.created", "Building created"),
        EventSubject("core.building.updated", "Building edited"),
        EventSubject("core.building.deleted", "Building deleted"),
        EventSubject("core.room.created", "Room created"),
        EventSubject("core.room.updated", "Room edited (incl. subnets)"),
        EventSubject("core.room.deleted", "Room deleted"),
        EventSubject("core.device.created", "Device registered"),
        EventSubject("core.device.updated", "Device edited or moved to another room"),  # ruff: ignore[line-too-long]
        EventSubject("core.device.deleted", "Device deleted"),
        EventSubject("core.policy.changed", "Anything that may change effective policies"),  # ruff: ignore[line-too-long]
        EventSubject("core.settings.changed", "A module's settings were changed"),  # ruff: ignore[line-too-long]
    )

    settings_model = CoreSettings

    async def dashboard(self, ctx: Ctx) -> list[Widget]:  # ruff: ignore[no-self-use, undocumented-public-method]
        return await core_widgets(ctx)

    def router(self) -> APIRouter:  # ruff: ignore[no-self-use, undocumented-public-method]
        r = APIRouter()
        for sub in (auth, users, groups, buildings, rooms, devices, policies, platform):
            r.include_router(sub.router)
        return r
