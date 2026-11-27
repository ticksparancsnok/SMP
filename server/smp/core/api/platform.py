# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Platform endpoints: module manifests, per-module settings, API tokens, audit log."""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError

from smp.core.api import ORMModel
from smp.core.context import Ctx, get_ctx, require
from smp.core.dashboard import audit_in_scope
from smp.core.db import utcnow
from smp.core.errors import AppError
from smp.core.models import ApiToken, AuditLog, ModuleSetting
from smp.core.module import Widget
from smp.core.security import new_token

router = APIRouter(tags=["platform"])
log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- dashboard


@router.get("/dashboard", response_model=list[Widget])
async def dashboard(ctx: Ctx = Depends(get_ctx)):
    """Home-page cards from every module, filtered by the caller's permissions.
    A failing module only loses its own cards."""
    widgets: list[Widget] = []
    for m in ctx.registry:
        try:
            widgets.extend(await m.dashboard(ctx))
        except Exception:
            log.exception("dashboard() of module %s failed", m.name)
            await ctx.db.rollback()
    return sorted(widgets, key=lambda w: w.order)


# --------------------------------------------------------------------------- modules


@router.get("/modules")
async def list_modules(ctx: Ctx = Depends(get_ctx)) -> list[dict[str, Any]]:
    """Manifests of all enabled modules. The UI builds its navigation from
    ui_routes, filtered by the permissions returned from /auth/me."""
    return [m.manifest() for m in ctx.registry]


def _settings_model(ctx: Ctx, name: str):
    mod = ctx.registry.get(name)
    if mod is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown module")
    if mod.settings_model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "module has no settings")
    return mod.settings_model


@router.get("/modules/{name}/settings")
async def get_module_settings(name: str, ctx: Ctx = Depends(require("core.settings.read"))) -> dict[str, Any]:
    _settings_model(ctx, name)
    return (await ctx.settings(name)).model_dump(mode="json")


@router.put("/modules/{name}/settings")
async def put_module_settings(
    name: str, body: dict[str, Any], ctx: Ctx = Depends(require("core.settings.write"))
) -> dict[str, Any]:
    model = _settings_model(ctx, name)
    try:
        data = model.model_validate(body).model_dump(mode="json")
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.errors(include_url=False)) from e
    row = await ctx.db.get(ModuleSetting, (ctx.org_id, name))
    if row is None:
        row = ModuleSetting(org_id=ctx.org_id, module=name)
        ctx.db.add(row)
    old = dict(row.data or {})
    row.data = data
    row.updated_by = ctx.principal.id
    ctx.audit("core.settings.update", target_type="module", target_id=name, details={"old": old, "new": data})
    ctx.emit("core.settings.changed", {"module": name})
    await ctx.commit()
    return data


# --------------------------------------------------------------------------- API tokens


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    permissions: list[str]
    expires_at: datetime | None = None


class TokenOut(ORMModel):
    id: uuid.UUID
    name: str
    prefix: str
    permissions: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenCreated(TokenOut):
    token: str  # shown exactly once


@router.get("/tokens", response_model=list[TokenOut])
async def list_tokens(ctx: Ctx = Depends(require("core.tokens.manage"))):
    return list((await ctx.db.execute(ctx.select(ApiToken).order_by(ApiToken.created_at.desc()))).scalars())


@router.post("/tokens", response_model=TokenCreated, status_code=201)
async def create_token(body: TokenIn, ctx: Ctx = Depends(require("core.tokens.manage"))):
    unknown = set(body.permissions) - set(ctx.registry.permissions)
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown permissions: {sorted(unknown)}")
    # A token can never hold more than its creator.
    for p in body.permissions:
        ctx.check(p)
    plain, digest = new_token("smpt")
    t = ApiToken(
        org_id=ctx.org_id,
        name=body.name,
        prefix=plain[:12],
        token_hash=digest,
        permissions=sorted(set(body.permissions)),
        created_by=ctx.principal.id,
        expires_at=body.expires_at,
    )
    ctx.db.add(t)
    await ctx.db.flush()
    ctx.audit("core.token.create", t, {"name": t.name, "permissions": t.permissions})
    await ctx.commit()
    return TokenCreated(**TokenOut.model_validate(t).model_dump(), token=plain)


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(token_id: uuid.UUID, ctx: Ctx = Depends(require("core.tokens.manage"))):
    t = await ctx.get(ApiToken, token_id)
    # Below an admin, only tokens that hold nothing the caller lacks (a unit manager
    # must not be able to disable an admin's token).
    p = ctx.principal
    assert p is not None
    if "*" not in p.global_perms and not set(t.permissions) <= set(p.all_permissions(ctx.registry)):
        raise AppError(
            status.HTTP_403_FORBIDDEN, "tokens.exceeds_permissions", "the token holds permissions you don't have"
        )
    if t.revoked_at is None:
        t.revoked_at = utcnow()
        ctx.audit("core.token.revoke", t, {"name": t.name})
        await ctx.commit()


# --------------------------------------------------------------------------- audit log


class AuditOut(ORMModel):
    id: int
    at: datetime
    actor_type: str
    actor_id: str | None
    actor_name: str
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    ip: str | None


@router.get("/audit", response_model=list[AuditOut])
async def list_audit(
    ctx: Ctx = Depends(require("core.audit.read", any_room=True)),
    action: str | None = Query(None, description="exact action, or prefix ending in '.' e.g. 'core.user.'"),
    actor_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before_id: int | None = Query(None, description="keyset pagination: pass the last id you received"),
    limit: int = Query(100, le=1000),
):
    stmt = ctx.select(AuditLog)
    assert ctx.principal is not None
    if (in_scope := audit_in_scope(ctx.principal)) is not None:
        stmt = stmt.where(in_scope)  # building managers: entries about their buildings only
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action) if action.endswith(".") else AuditLog.action == action)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if since:
        stmt = stmt.where(AuditLog.at >= since)
    if until:
        stmt = stmt.where(AuditLog.at < until)
    if before_id:
        stmt = stmt.where(AuditLog.id < before_id)
    return list((await ctx.db.execute(stmt.order_by(AuditLog.id.desc()).limit(limit))).scalars())
