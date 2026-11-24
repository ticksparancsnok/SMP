# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Request context: who is calling (Principal) and the toolbox handlers use (Ctx).

Typical module handler::

    @router.post("/things")
    async def create_thing(body: ThingIn, ctx: Ctx = Depends(require("mymod.things.write"))):
        thing = Thing(org_id=ctx.org_id, name=body.name)
        ctx.db.add(thing)
        await ctx.db.flush()
        ctx.audit("mymod.thing.create", thing, {"name": thing.name})
        ctx.emit("mymod.thing.created", {"id": str(thing.id)})
        await ctx.commit()          # commits, then publishes queued events
        return thing
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypeVar
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from smp.core.config import get_settings
from smp.core.db import get_session, utcnow
from smp.core.errors import AppError
from smp.core.events import Event, EventBus
from smp.core.models import ApiToken, AuditLog, AuthSession, ModuleSetting, Organization, User
from smp.core.module import ModuleRegistry
from smp.core.security import hash_token

T = TypeVar("T")

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# The only endpoints a user may call until they replace an admin-set password.
# (/auth/logout doesn't go through get_principal at all.) /auth/preferences only stores
# the user's own UI settings: the language switch is on the password-change screen too.
_ALLOWED_DURING_PASSWORD_CHANGE = {
    "/api/v1/auth/me",
    "/api/v1/auth/password",
    "/api/v1/auth/preferences",
    "/api/v1/modules",
}
_TOUCH_INTERVAL = timedelta(minutes=1)


@dataclass
class Principal:
    """An authenticated caller: a user (browser session) or a service (API token)."""

    kind: str  # "user" | "token"
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    global_perms: frozenset[str]
    room_perms: dict[uuid.UUID, frozenset[str]] = field(default_factory=dict)
    roles: list[tuple[str, uuid.UUID | None]] = field(default_factory=list)
    must_change_password: bool = False

    def can(self, perm: str, room_id: uuid.UUID | None = None) -> bool:
        """Org-wide check, or (when room_id is given) org-wide OR granted for that room."""
        if "*" in self.global_perms or perm in self.global_perms:
            return True
        return room_id is not None and perm in self.room_perms.get(room_id, frozenset())

    def can_anywhere(self, perm: str) -> bool:
        return self.can(perm) or any(perm in p for p in self.room_perms.values())

    def rooms_with(self, perm: str) -> set[uuid.UUID] | None:
        """Rooms where perm applies; None means all rooms (org-wide grant).
        Use it to filter list endpoints."""
        if self.can(perm):
            return None
        return {room for room, perms in self.room_perms.items() if perm in perms}

    def all_permissions(self, registry: ModuleRegistry) -> list[str]:
        if "*" in self.global_perms:
            return sorted(registry.permissions)
        return sorted(self.global_perms)


class Ctx:
    def __init__(
        self,
        request: Request,
        db: AsyncSession,
        principal: Principal | None,
        org_id: uuid.UUID,
    ):
        self.request = request
        self.db = db
        self.principal = principal
        self.org_id = org_id
        self.registry: ModuleRegistry = request.app.state.registry
        self.bus: EventBus = request.app.state.bus
        self._pending: list[Event] = []

    # ---- authorization
    def check(self, perm: str, room_id: uuid.UUID | None = None) -> None:
        if self.principal is None or not self.principal.can(perm, room_id):
            raise AppError(status.HTTP_403_FORBIDDEN, "permission.missing", f"missing permission {perm}", permission=perm)

    # ---- org-scoped data access
    def select(self, model: type[T], *extra: Any) -> Select[tuple[T]]:
        """select(model) already filtered to this org."""
        return select(model, *extra).where(model.org_id == self.org_id)  # type: ignore[attr-defined]

    async def get(self, model: type[T], id: uuid.UUID) -> T:
        """Fetch by primary key within this org, or 404."""
        obj = await self.db.get(model, id)
        if obj is None or obj.org_id != self.org_id:  # type: ignore[attr-defined]
            raise AppError(status.HTTP_404_NOT_FOUND, "not_found", f"{model.__name__} not found", entity=model.__name__)
        return obj

    async def get_org(self) -> Organization:
        org = await self.db.get(Organization, self.org_id)
        assert org is not None
        return org

    # ---- settings
    async def settings(self, module: str) -> BaseModel:
        mod = self.registry.get(module)
        if mod is None or mod.settings_model is None:
            raise KeyError(f"module {module!r} has no settings")
        row = await self.db.get(ModuleSetting, (self.org_id, module))
        return mod.settings_model.model_validate(row.data if row else {})

    # ---- audit & events
    def audit(
        self,
        action: str,
        target: Any = None,
        details: dict[str, Any] | None = None,
        *,
        target_type: str | None = None,
        target_id: Any = None,
    ) -> None:
        """Record an audit entry in the current transaction (committed with the change).
        `target` may be an ORM object; its class name and id are recorded."""
        if target is not None:
            target_type = target_type or type(target).__name__.lower()
            target_id = target_id if target_id is not None else getattr(target, "id", None)
        p = self.principal
        self.db.add(
            AuditLog(
                org_id=self.org_id,
                actor_type=p.kind if p else "anonymous",
                actor_id=str(p.id) if p else None,
                actor_name=p.name if p else "",
                action=action,
                target_type=target_type,
                target_id=str(target_id) if target_id is not None else None,
                details=details or {},
                ip=client_ip(self.request),
                user_agent=(self.request.headers.get("user-agent") or "")[:400] or None,
            )
        )

    def emit(self, subject: str, payload: dict[str, Any]) -> None:
        """Queue an event; it is published only after commit() succeeds."""
        self._pending.append(Event(subject=subject, org_id=self.org_id, payload=payload))

    async def commit(self) -> None:
        await self.db.commit()
        pending, self._pending = self._pending, []
        for ev in pending:
            await self.bus.publish(ev)


def client_ip(request: Request) -> str | None:
    # Behind a reverse proxy, run uvicorn with --forwarded-allow-ips so this is the real client.
    return request.client.host if request.client else None


# --------------------------------------------------------------------------- authentication


def principal_for_user(user: User, registry: ModuleRegistry) -> Principal:
    global_perms: set[str] = set()
    room_perms: dict[uuid.UUID, set[str]] = {}
    for ra in user.roles:
        perms = registry.role_permissions(ra.role)
        if ra.room_id is None:
            global_perms |= perms
        else:
            # Room assignment: room-scoped permissions only for that room, the rest org-wide.
            scoped = {p for p in perms if registry.is_room_scoped(p)}
            room_perms.setdefault(ra.room_id, set()).update(scoped)
            global_perms |= perms - scoped
    return Principal(
        kind="user",
        id=user.id,
        org_id=user.org_id,
        name=user.username,
        global_perms=frozenset(global_perms),
        room_perms={k: frozenset(v) for k, v in room_perms.items()},
        roles=[(ra.role, ra.room_id) for ra in user.roles],
        must_change_password=user.must_change_password,
    )


def check_origin(request: Request) -> None:
    """CSRF defence for cookie auth (in addition to SameSite=Strict): unsafe requests
    carrying an Origin header must come from our own host."""
    origin = request.headers.get("origin")
    if request.method in _SAFE_METHODS or not origin:
        return
    allowed = {o.strip() for o in get_settings().cors_origins.split(",") if o.strip()}
    if origin in allowed:
        return
    if urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-origin request rejected")


async def _principal_from_bearer(db: AsyncSession, token: str, registry: ModuleRegistry) -> Principal | None:
    row = (await db.execute(select(ApiToken).where(ApiToken.token_hash == hash_token(token)))).scalar_one_or_none()
    now = utcnow()
    if row is None or row.revoked_at is not None or (row.expires_at and row.expires_at <= now):
        return None
    if row.last_used_at is None or now - row.last_used_at > _TOUCH_INTERVAL:
        row.last_used_at = now
        await db.commit()
    perms = frozenset(p for p in row.permissions if p in registry.permissions)
    return Principal(kind="token", id=row.id, org_id=row.org_id, name=f"token:{row.name}", global_perms=perms)


async def _principal_from_cookie(db: AsyncSession, token: str, registry: ModuleRegistry) -> Principal | None:
    sess = (
        await db.execute(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    now = utcnow()
    if sess is None or sess.expires_at <= now:
        return None
    user = await db.get(User, sess.user_id)
    if user is None or user.disabled:
        return None
    if now - sess.last_seen_at > _TOUCH_INTERVAL:
        sess.last_seen_at = now
        await db.commit()
    return principal_for_user(user, registry)


async def get_principal(request: Request, db: AsyncSession = Depends(get_session)) -> Principal:
    registry: ModuleRegistry = request.app.state.registry
    auth = request.headers.get("authorization", "")
    principal: Principal | None = None
    if auth.lower().startswith("bearer "):
        principal = await _principal_from_bearer(db, auth[7:].strip(), registry)
    elif cookie := request.cookies.get(get_settings().session_cookie_name):
        check_origin(request)
        principal = await _principal_from_cookie(db, cookie, registry)
    if principal is None:
        raise AppError(status.HTTP_401_UNAUTHORIZED, "auth.not_authenticated", "not authenticated")
    if principal.must_change_password and request.url.path not in _ALLOWED_DURING_PASSWORD_CHANGE:
        raise AppError(status.HTTP_403_FORBIDDEN, "auth.password_change_required", "password change required")
    return principal


async def get_ctx(
    request: Request,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> Ctx:
    return Ctx(request, db, principal, principal.org_id)


def websocket_origin_ok(websocket: Any) -> bool:
    """A browser WebSocket must come from a page of ours. WebSockets aren't covered by
    CORS, so (unlike check_origin for requests) a missing Origin is refused too:
    browsers always send one."""
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    allowed = {o.strip() for o in get_settings().cors_origins.split(",") if o.strip()}
    return origin in allowed or urlsplit(origin).netloc == websocket.headers.get("host")


async def websocket_ctx(websocket: Any, db: AsyncSession) -> Ctx | None:
    """The signed-in user of a browser WebSocket (session cookie, our own Origin), as a
    Ctx for permission checks and audit entries; None if not allowed. `websocket` is a
    Starlette WebSocket: it has the headers, cookies, client and app a Ctx uses."""
    if not websocket_origin_ok(websocket):
        return None
    cookie = websocket.cookies.get(get_settings().session_cookie_name)
    if not cookie:
        return None
    principal = await _principal_from_cookie(db, cookie, websocket.app.state.registry)
    if principal is None or principal.must_change_password:
        return None
    return Ctx(websocket, db, principal, principal.org_id)


def require(*perms: str, any_room: bool = False) -> Callable[..., Awaitable[Ctx]]:
    """Dependency factory: authenticate and check permissions.

    any_room=False (default): the permission must be granted org-wide.
    any_room=True: a grant for at least one room is enough; the handler MUST then
    filter/check per room with ctx.principal.rooms_with(...) / ctx.check(perm, room_id).
    """

    async def dep(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        assert ctx.principal is not None
        for perm in perms:
            ok = ctx.principal.can_anywhere(perm) if any_room else ctx.principal.can(perm)
            if not ok:
                raise AppError(status.HTTP_403_FORBIDDEN, "permission.missing", f"missing permission {perm}", permission=perm)
        return ctx

    return dep


async def public_ctx(request: Request, db: AsyncSession = Depends(get_session)) -> Ctx:
    """Unauthenticated context bound to the default org (for public routes such as
    short-link redirects)."""
    org = (
        await db.execute(select(Organization).where(Organization.slug == get_settings().default_org))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "not bootstrapped")
    return Ctx(request, db, None, org.id)
