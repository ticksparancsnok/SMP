# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""DNS blocklist and allowlist (decision order and domain rules: domains.py)."""

import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smp.core.api import Page
from smp.core.config import get_settings
from smp.core.context import Ctx, require
from smp.core.errors import AppError
from smp.modules.dns.domains import (
    blocked_by,
    by_domain,
    is_public_suffix,
    match,
    normalize_domain,
    parse_hosts,
    public_suffix_count,
    public_suffix_source,
)
from smp.modules.dns.models import DnsAllowlistEntry, DnsBlocklistEntry

router = APIRouter()

# The same permissions cover both lists: whoever may block a site may also unblock one.
READ = "dns.blocklist.read"
WRITE = "dns.blocklist.write"


class EntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1, max_length=253)
    include_subdomains: bool = True
    reason: str = Field("", max_length=500)
    confidence: float | None = Field(default=None, ge=0, le=1)


class AllowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1, max_length=253)
    include_subdomains: bool = True
    reason: str = Field("", max_length=500)


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    domain: str
    includeSubdomains: bool
    reason: str
    addedBy: str
    confidence: float | None
    addedAt: datetime
    # False when the entry can't take effect: it names a public suffix (created before
    # those were refused) or the allowlist covers it ("allowlisted").
    active: bool = True
    inactiveReason: str | None = None  # "public_suffix" | "allowlisted"


class AllowEntryOut(BaseModel):
    id: str
    domain: str
    includeSubdomains: bool
    reason: str
    addedBy: str
    addedAt: datetime


class AllowlistOut(BaseModel):
    entries: list[AllowEntryOut]
    # The built-in part of the allowlist: public suffixes that can never be blocked.
    public_suffixes: int
    public_suffix_source: str


class HostsPreviewOut(BaseModel):
    domains: list[str]
    unique_count: int
    # Valid domains left out because the allowlist covers them or they are public suffixes.
    skipped: list[str] = []


class SyncStatusOut(BaseModel):
    entries: int
    allowlist_entries: int = 0
    sinkhole_url: str
    sync_configured: bool


def row_to_out(row: DnsBlocklistEntry, allow: dict | None = None) -> EntryOut:
    reason = None
    if is_public_suffix(row.domain):
        reason = "public_suffix"
    elif allow and match(row.domain, allow) is not None:
        reason = "allowlisted"
    return EntryOut(
        id=str(row.id),
        domain=row.domain,
        includeSubdomains=row.include_subdomains,
        reason=row.reason,
        addedBy=row.added_by,
        confidence=row.confidence,
        addedAt=row.added_at,
        active=reason is None,
        inactiveReason=reason,
    )


def allow_to_out(row: DnsAllowlistEntry) -> AllowEntryOut:
    return AllowEntryOut(
        id=str(row.id),
        domain=row.domain,
        includeSubdomains=row.include_subdomains,
        reason=row.reason,
        addedBy=row.added_by,
        addedAt=row.added_at,
    )


async def _blocklist(ctx: Ctx) -> list[DnsBlocklistEntry]:
    rows = await ctx.db.execute(ctx.select(DnsBlocklistEntry).order_by(DnsBlocklistEntry.domain))
    return list(rows.scalars())


async def _allowlist(ctx: Ctx) -> list[DnsAllowlistEntry]:
    rows = await ctx.db.execute(ctx.select(DnsAllowlistEntry).order_by(DnsAllowlistEntry.domain))
    return list(rows.scalars())


def _wire(row: DnsBlocklistEntry | DnsAllowlistEntry) -> dict:
    out = {
        "domain": row.domain,
        "includeSubdomains": row.include_subdomains,
        "reason": row.reason,
        "addedBy": row.added_by,
        "addedAt": row.added_at.isoformat(),
    }
    if isinstance(row, DnsBlocklistEntry):
        out["confidence"] = row.confidence
    return out


async def sync_payload(db: AsyncSession, org_id: uuid.UUID) -> dict:
    """The full state the sinkhole should have: both lists of the school.
    Public-suffix blocklist entries (legacy rows) are never sent."""
    block = await db.execute(
        select(DnsBlocklistEntry).where(DnsBlocklistEntry.org_id == org_id).order_by(DnsBlocklistEntry.domain)
    )
    allow = await db.execute(
        select(DnsAllowlistEntry).where(DnsAllowlistEntry.org_id == org_id).order_by(DnsAllowlistEntry.domain)
    )
    return {
        "entries": [_wire(r) for r in block.scalars() if not is_public_suffix(r.domain)],
        "allowlist": [_wire(r) for r in allow.scalars()],
    }


async def push_to_sinkhole(payload: dict) -> None:
    """Replace the Go sinkhole's lists (it applies allowlist -> blocklist -> allow).
    Raises httpx.HTTPError if it can't be reached or refuses the token."""
    settings = get_settings()
    headers = {}
    if settings.dns_sync_token:
        headers["Authorization"] = f"Bearer {settings.dns_sync_token}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(
            settings.dns_sinkhole_url.rstrip("/") + "/api/blocklist/sync",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()


async def sync_sinkhole(ctx: Ctx) -> None:
    """After a change: push the school's full state (also: `smp dns-sync`)."""
    try:
        await push_to_sinkhole(await sync_payload(ctx.db, ctx.org_id))
    except httpx.HTTPError as exc:
        raise AppError(
            status.HTTP_502_BAD_GATEWAY,
            "dns.sync_failed",
            "blocklist was saved, but the DNS sinkhole could not be synchronized",
        ) from exc


def _valid_domain(raw: str) -> str:
    domain = normalize_domain(raw)
    if domain is None:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "dns.invalid_domain", "invalid domain")
    if is_public_suffix(domain):
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "dns.public_suffix",
            f"{domain} is a public suffix (like com or co.uk); it can't be blocked or allowed as a whole",
            domain=domain,
        )
    return domain


def _parse_id(entry_id: str, entity: str) -> uuid.UUID:
    try:
        return uuid.UUID(entry_id)
    except ValueError as exc:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", f"{entity} not found", entity=entity) from exc


# --------------------------------------------------------------------------- blocklist


@router.get("/blocklist", response_model=Page[EntryOut])
async def list_blocklist(
    ctx: Ctx = Depends(require(READ)),
    q: str | None = Query(None, max_length=253, description="substring of the domain, reason or added-by"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[EntryOut]:
    """One page of the blocklist, by domain. The list can hold 100k+ imported domains,
    so it is never sent whole (the sinkhole gets it through sync_payload())."""
    stmt = ctx.select(DnsBlocklistEntry)
    if q and (q := q.strip()):
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                DnsBlocklistEntry.domain.ilike(like),
                DnsBlocklistEntry.reason.ilike(like),
                DnsBlocklistEntry.added_by.ilike(like),
            )
        )
    total = await ctx.db.scalar(stmt.with_only_columns(func.count()).order_by(None))
    rows = await ctx.db.execute(stmt.order_by(DnsBlocklistEntry.domain).limit(limit).offset(offset))
    allow = by_domain(await _allowlist(ctx))  # small: hand-picked exceptions
    return Page(items=[row_to_out(row, allow) for row in rows.scalars()], total=total or 0)


@router.post("/blocklist", response_model=EntryOut, status_code=201)
async def create_entry(body: EntryIn, ctx: Ctx = Depends(require(WRITE))):
    domain = _valid_domain(body.domain)
    allow = by_domain(await _allowlist(ctx))
    if (covering := match(domain, allow)) is not None:
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "dns.allowlisted",
            f"{domain} is on the allowlist ({covering.domain}); remove it there first",
            domain=domain,
            allowlist_entry=covering.domain,
        )
    row = DnsBlocklistEntry(
        org_id=ctx.org_id,
        domain=domain,
        include_subdomains=body.include_subdomains,
        reason=body.reason,
        added_by=ctx.principal.name,
        confidence=body.confidence,
        added_at=datetime.now(UTC),
    )
    ctx.db.add(row)
    try:
        await ctx.db.flush()
    except IntegrityError as exc:
        await ctx.db.rollback()
        raise AppError(status.HTTP_409_CONFLICT, "dns.domain_exists", "domain already exists") from exc
    ctx.audit("dns.blocklist.create", row, {"domain": domain, "addedBy": ctx.principal.name})
    ctx.emit("dns.blocklist.updated", {"domain": domain})
    await ctx.commit()
    await sync_sinkhole(ctx)
    return row_to_out(row, allow)


@router.delete("/blocklist/{entry_id}", status_code=204)
async def delete_entry(entry_id: str, ctx: Ctx = Depends(require(WRITE))):
    row = await ctx.get(DnsBlocklistEntry, _parse_id(entry_id, "DnsBlocklistEntry"))
    ctx.audit("dns.blocklist.delete", row, {"domain": row.domain})
    ctx.emit("dns.blocklist.updated", {"domain": row.domain})
    await ctx.db.delete(row)
    await ctx.commit()
    await sync_sinkhole(ctx)


async def _importable(ctx: Ctx, content: bytes) -> tuple[list[str], list[str]]:
    """(domains to block, valid domains skipped because allowlisted / public suffix)."""
    allow = by_domain(await _allowlist(ctx))
    take, skipped = [], []
    for domain in parse_hosts(content):
        (skipped if is_public_suffix(domain) or match(domain, allow) else take).append(domain)
    return take, skipped


@router.post("/blocklist/import-preview", response_model=HostsPreviewOut)
async def preview_import(request: Request, ctx: Ctx = Depends(require(WRITE))):
    domains, skipped = await _importable(ctx, await request.body())
    return HostsPreviewOut(domains=domains, unique_count=len(domains), skipped=skipped)


@router.post("/blocklist/import", response_model=HostsPreviewOut, status_code=201)
async def import_hosts(request: Request, ctx: Ctx = Depends(require(WRITE))):
    domains, skipped = await _importable(ctx, await request.body())
    if not domains:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "dns.no_valid_domains", "no valid domains found")
    existing = set((await ctx.db.execute(select(DnsBlocklistEntry.domain).where(DnsBlocklistEntry.org_id == ctx.org_id))).scalars())
    now = datetime.now(UTC)
    added_by = f"imported by: {ctx.principal.name}"
    new = [d for d in domains if d not in existing]
    ctx.db.add_all(
        DnsBlocklistEntry(
            org_id=ctx.org_id,
            domain=d,
            include_subdomains=True,
            reason="imported",
            added_by=added_by,
            confidence=None,
            added_at=now,
        )
        for d in new
    )
    ctx.audit(
        "dns.blocklist.import",
        target_type="dns_blocklist",
        details={"imported": len(new), "skipped": len(skipped), "addedBy": added_by},
    )
    ctx.emit("dns.blocklist.updated", {"imported": len(new)})
    await ctx.commit()
    await sync_sinkhole(ctx)
    return HostsPreviewOut(domains=domains, unique_count=len(new), skipped=skipped)


# --------------------------------------------------------------------------- allowlist


@router.get("/allowlist", response_model=AllowlistOut)
async def list_allowlist(ctx: Ctx = Depends(require(READ))):
    return AllowlistOut(
        entries=[allow_to_out(r) for r in await _allowlist(ctx)],
        public_suffixes=public_suffix_count(),
        public_suffix_source=public_suffix_source(),
    )


@router.post("/allowlist", response_model=AllowEntryOut, status_code=201)
async def create_allow_entry(body: AllowIn, ctx: Ctx = Depends(require(WRITE))):
    domain = _valid_domain(body.domain)
    row = DnsAllowlistEntry(
        org_id=ctx.org_id,
        domain=domain,
        include_subdomains=body.include_subdomains,
        reason=body.reason,
        added_by=ctx.principal.name,
        added_at=datetime.now(UTC),
    )
    ctx.db.add(row)
    try:
        await ctx.db.flush()
    except IntegrityError as exc:
        await ctx.db.rollback()
        raise AppError(status.HTTP_409_CONFLICT, "dns.allow_exists", "domain is already on the allowlist") from exc
    ctx.audit("dns.allowlist.create", row, {"domain": domain, "include_subdomains": row.include_subdomains})
    ctx.emit("dns.allowlist.updated", {"domain": domain})
    await ctx.commit()
    await sync_sinkhole(ctx)
    return allow_to_out(row)


@router.delete("/allowlist/{entry_id}", status_code=204)
async def delete_allow_entry(entry_id: str, ctx: Ctx = Depends(require(WRITE))):
    row = await ctx.get(DnsAllowlistEntry, _parse_id(entry_id, "DnsAllowlistEntry"))
    ctx.audit("dns.allowlist.delete", row, {"domain": row.domain})
    ctx.emit("dns.allowlist.updated", {"domain": row.domain})
    await ctx.db.delete(row)
    await ctx.commit()
    await sync_sinkhole(ctx)


@router.get("/check", response_model=dict)
async def check_domain(domain: str, ctx: Ctx = Depends(require(READ))) -> dict:
    """Would this domain be blocked right now? (Same decision as the sinkhole.)"""
    d = normalize_domain(domain)
    if d is None:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "dns.invalid_domain", "invalid domain")
    allow, block = by_domain(await _allowlist(ctx)), by_domain(await _blocklist(ctx))
    entry = blocked_by(d, allow, block)
    covering = match(d, allow)
    return {
        "domain": d,
        "blocked": entry is not None,
        "blocked_by": entry.domain if entry else None,
        "allowed_by": "public_suffix" if is_public_suffix(d) else (covering.domain if covering else None),
    }


@router.get("/status", response_model=SyncStatusOut)
async def sync_status(ctx: Ctx = Depends(require(READ))):
    count = await ctx.db.scalar(select(func.count()).where(DnsBlocklistEntry.org_id == ctx.org_id))
    allowed = await ctx.db.scalar(select(func.count()).where(DnsAllowlistEntry.org_id == ctx.org_id))
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            headers = {}
            if settings.dns_sync_token:
                headers["Authorization"] = f"Bearer {settings.dns_sync_token}"
            response = await client.get(
                settings.dns_sinkhole_url.rstrip("/") + "/api/blocklist/status",
                headers=headers,
            )
            # 200 only when the sinkhole is reachable *and* accepts our sync token.
            sync_configured = response.status_code == 200
    except httpx.HTTPError:
        sync_configured = False
    return SyncStatusOut(
        entries=count or 0,
        allowlist_entries=allowed or 0,
        sinkhole_url=settings.dns_sinkhole_url,
        sync_configured=sync_configured,
    )
