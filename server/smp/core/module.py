"""Module contract and registry.

A module is a Python package under smp/modules/ that exposes a subclass of `Module`.
Modules are compiled in (imported explicitly in smp/modules/__init__.py), not loaded
at runtime; this keeps things simple while the interfaces stay clean enough to move a
module into its own process later.

What a module declares (its "manifest"):
  - permissions   : RBAC permission codes it checks, with default role grants
  - settings_model: pydantic model for per-school settings (edited in the portal)
  - policy_model  : pydantic model for Policy.data when Policy.kind == module name
  - ui_routes     : navigation entries for the web UI
  - events        : event subjects it publishes
What a module provides (optional hooks):
  - router()          : mounted under /api/v1/<name>
  - public_router()   : mounted at the site root (e.g. /s/{code} for short links)
  - subscribe(bus)    : register event handlers
  - identity_providers(): extra login backends (ldap, google, ...)
  - dashboard(ctx)    : cards for the portal's home page
  - agent_config(db, device): this module's section of the config the device agent
                        receives (e.g. "screen": interval, size); None = nothing
  - startup()/shutdown()
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi import APIRouter
    from sqlalchemy.ext.asyncio import AsyncSession

    from smp.core.context import Ctx
    from smp.core.events import EventBus
    from smp.core.identity import IdentityProvider
    from smp.core.models import Device

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


@dataclass(frozen=True)
class Permission:
    code: str  # "<module>.<thing>.<verb>", e.g. "dns.blocklist.write"
    description: str
    default_roles: tuple[str, ...] = ()  # roles that get it out of the box ("admin" always does)
    # True for permissions about things that live in a room (devices, screens, room
    # policies). A role assigned for one room grants room-scoped permissions only in
    # that room; its other permissions (e.g. create short links) apply org-wide.
    room_scoped: bool = False


@dataclass(frozen=True)
class UiRoute:
    path: str  # frontend route, e.g. "/dns"
    title: str
    icon: str = ""
    permission: str | None = None  # hide the entry unless the user has this
    section: str = "services"  # "overview" | "admin" | "services" | ...


@dataclass(frozen=True)
class EventSubject:
    subject: str  # "<module>.<entity>.<event>", e.g. "dns.domain.blocked"
    description: str


class WidgetItem(BaseModel):
    label: str
    value: str = ""
    link: str | None = None
    at: datetime | None = None


class Widget(BaseModel):
    """A home-page card. "stat" shows one big value, "list" shows `items`.
    The whole card links to `link` (a frontend route, may include a query string)."""

    id: str  # "<module>.<name>", e.g. "dns.top_blocked"
    kind: Literal["stat", "list"] = "stat"
    title: str  # English; the UI translates by `id`
    value: int | float | str | None = None
    hint: str = ""  # English; the UI rebuilds it in the user's language from `data`
    data: dict[str, int | float | str] = {}  # the numbers behind `hint`
    icon: str = ""
    link: str | None = None
    items: list[WidgetItem] = []
    order: int = 100  # lower comes first


class Module:
    name: ClassVar[str]
    title: ClassVar[str]
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = ""
    permissions: ClassVar[Sequence[Permission]] = ()
    ui_routes: ClassVar[Sequence[UiRoute]] = ()
    events: ClassVar[Sequence[EventSubject]] = ()
    settings_model: ClassVar[type[BaseModel] | None] = None
    policy_model: ClassVar[type[BaseModel] | None] = None

    def router(self) -> APIRouter | None:
        return None

    def public_router(self) -> APIRouter | None:
        return None

    def subscribe(self, bus: EventBus) -> None:  # noqa: B027 - optional hook
        pass

    def identity_providers(self) -> Sequence[IdentityProvider]:
        return ()

    async def dashboard(self, ctx: Ctx) -> Sequence[Widget]:
        """Home-page cards for the calling user. Check permissions here: only
        return what ctx.principal is allowed to see."""
        return ()

    async def agent_config(self, db: AsyncSession, device: Device) -> dict[str, Any] | None:
        """This module's section of a device agent's config (see modules/agent). Return
        the same answer for the same state (database, or in-memory state such as who is
        watching a room): the agent module hashes the whole config to tell agents
        whether anything changed. Publish `agent.config.changed` when that state
        changes, so waiting agents ask again at once."""
        return None

    async def startup(self) -> None:  # noqa: B027
        pass

    async def shutdown(self) -> None:  # noqa: B027
        pass

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "description": self.description,
            "permissions": [
                {
                    "code": p.code,
                    "description": p.description,
                    "default_roles": list(p.default_roles),
                    "room_scoped": p.room_scoped,
                }
                for p in self.permissions
            ],
            "ui_routes": [r.__dict__ for r in self.ui_routes],
            "events": [e.__dict__ for e in self.events],
            "settings_schema": self.settings_model.model_json_schema() if self.settings_model else None,
            "policy_schema": self.policy_model.model_json_schema() if self.policy_model else None,
        }


class ModuleRegistry:
    """The set of enabled modules plus everything derived from their manifests."""

    def __init__(self, modules: Iterable[Module]):
        self._modules: dict[str, Module] = {}
        self._permissions: dict[str, Permission] = {}
        for m in modules:
            self._add(m)
        self._role_perms: dict[str, frozenset[str]] = {}
        for p in self._permissions.values():
            for role in p.default_roles:
                self._role_perms[role] = self._role_perms.get(role, frozenset()) | {p.code}

    def _add(self, m: Module) -> None:
        if not _NAME_RE.match(m.name):
            raise ValueError(f"invalid module name {m.name!r}")
        if m.name in self._modules:
            raise ValueError(f"duplicate module {m.name!r}")
        for p in m.permissions:
            if not p.code.startswith(m.name + "."):
                raise ValueError(f"permission {p.code!r} must start with '{m.name}.'")
            if p.code in self._permissions:
                raise ValueError(f"duplicate permission {p.code!r}")
            self._permissions[p.code] = p
        for e in m.events:
            if not e.subject.startswith(m.name + "."):
                raise ValueError(f"event subject {e.subject!r} must start with '{m.name}.'")
        self._modules[m.name] = m

    def __iter__(self):
        return iter(self._modules.values())

    def get(self, name: str) -> Module | None:
        return self._modules.get(name)

    @property
    def permissions(self) -> dict[str, Permission]:
        return dict(self._permissions)

    def is_room_scoped(self, perm: str) -> bool:
        p = self._permissions.get(perm)
        return p is not None and p.room_scoped

    def role_permissions(self, role: str) -> frozenset[str]:
        """Permissions granted by a role. Admin is a wildcard."""
        if role == "admin":
            return frozenset({"*"})
        return self._role_perms.get(role, frozenset())

    def policy_model(self, kind: str) -> type[BaseModel] | None:
        m = self._modules.get(kind)
        return m.policy_model if m else None
