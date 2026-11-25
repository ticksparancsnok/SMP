# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""DNS blocklist and allowlist (decision order and domain rules: domains.py; hosts-file
imports: imports.py).

The blocklist can hold 100k+ imported domains, so nothing here loads it whole as ORM
objects on the event loop: pages are paged in SQL, a check looks up only the domain and
its parents, and for the sinkhole sync Postgres builds the JSON itself; one sync per
school at a time."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ARRAY, String, bindparam, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smp.core import db
from smp.core.api import Page
from smp.core.config import get_settings
from smp.core.context import Ctx, client_ip, require
from smp.core.errors import AppError
from smp.core.models import ModuleSetting
from smp.core.uploads import read_limited
from smp.modules.dns import imports
from smp.modules.dns.domains import (
    BUILTIN_BLOCKLIST,
    BuiltinBlock,
    blocked_by,
    by_domain,
    is_public_suffix,
    match,
    normalize_domain,
    plain_public_suffixes,
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


class ImportErrorOut(BaseModel):
    code: str
    detail: str
    params: dict = {}


class ImportJobOut(BaseModel):
    id: str
    state: Literal["checking", "ready", "saving", "syncing", "done", "failed"]
    progress: float  # of the current state, 0..1
    progress_current: int
    progress_total: int
    file_bytes: int
    new: int  # domains the import adds
    existing: int  # already on the blocklist
    skipped: int  # allowlisted or public suffixes: never blocked
    added: int | None = None  # after saving
    sync_ok: bool | None = None  # after syncing: False = saved, but the sinkhole didn't take it yet
    error: ImportErrorOut | None = None


class SyncStatusOut(BaseModel):
    entries: int
    allowlist_entries: int = 0
    # The built-in part of the blocklist (domains.BUILTIN_BLOCKLIST, with subdomains):
    # DNS-over-HTTPS servers and iCloud Private Relay, blocked unless allowlisted.
    builtin_blocklist: list[str] = []
    sinkhole_url: str
    sync_configured: bool
    # The block page's default language (DNS settings; admins change it on the Site
    # blocker page, visitors can switch it on the page itself).
    block_page_language: Literal["hu", "en"] = "hu"


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


async def _allowlist(ctx: Ctx) -> list[DnsAllowlistEntry]:
    rows = await ctx.db.execute(ctx.select(DnsAllowlistEntry).order_by(DnsAllowlistEntry.domain))
    return list(rows.scalars())


class SyncPayload(BaseModel):
    """What goes to the sinkhole: the JSON, already encoded, and how many entries."""

    body: bytes
    entries: int
    allowlist: int


# Postgres builds the JSON: 100k rows never become Python objects (that took over a second
# of the event loop). Same fields as the sinkhole's Entry (Site-blocker/.../blocklist.go).
_SYNC_SQL = text("""
    SELECT
      (SELECT count(*) FROM dns_blocklist_entries b WHERE b.org_id = :org AND b.domain <> ALL(:suffixes)),
      (SELECT coalesce(json_agg(json_build_object(
                'domain', b.domain, 'includeSubdomains', b.include_subdomains, 'reason', b.reason,
                'addedBy', b.added_by, 'addedAt', b.added_at, 'confidence', b.confidence) ORDER BY b.domain), '[]')::text
         FROM dns_blocklist_entries b WHERE b.org_id = :org AND b.domain <> ALL(:suffixes)),
      (SELECT count(*) FROM dns_allowlist_entries a WHERE a.org_id = :org),
      (SELECT coalesce(json_agg(json_build_object(
                'domain', a.domain, 'includeSubdomains', a.include_subdomains, 'reason', a.reason,
                'addedBy', a.added_by, 'addedAt', a.added_at) ORDER BY a.domain), '[]')::text
         FROM dns_allowlist_entries a WHERE a.org_id = :org)
""").bindparams(bindparam("suffixes", type_=ARRAY(String)))


async def sync_payload(s: AsyncSession, org_id: uuid.UUID) -> SyncPayload:
    """The full state the sinkhole should have: both lists of the school.
    Public-suffix blocklist entries (legacy rows) are never sent."""
    row = (await s.execute(_SYNC_SQL, {"org": org_id, "suffixes": list(plain_public_suffixes())})).one()
    n_block, block_json, n_allow, allow_json = row
    language = (await _dns_settings(s, org_id)).block_page_language  # "hu" | "en": safe to inline
    body = (b'{"entries":' + block_json.encode() + b',"allowlist":' + allow_json.encode()
            + b',"blockPageLanguage":"' + language.encode() + b'"}')
    return SyncPayload(body=body, entries=n_block, allowlist=n_allow)


async def _dns_settings(s: AsyncSession, org_id: uuid.UUID):
    from smp.modules.dns import DnsSettings

    row = await s.get(ModuleSetting, (org_id, "dns"))
    return DnsSettings.model_validate(row.data if row else {})


async def push_to_sinkhole(payload: SyncPayload) -> None:
    """Replace the Go sinkhole's lists (it applies allowlist -> blocklist -> allow).
    Raises httpx.HTTPError if it can't be reached or refuses the token."""
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    if settings.dns_sync_token:
        headers["Authorization"] = f"Bearer {settings.dns_sync_token}"
    # A big list is ~15 MB the sinkhole parses and writes to disk: give it time.
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
        response = await client.post(
            settings.dns_sinkhole_url.rstrip("/") + "/api/blocklist/sync",
            content=payload.body,
            headers=headers,
        )
        response.raise_for_status()


_sync_locks: dict[uuid.UUID, asyncio.Lock] = {}


async def sync_org(org_id: uuid.UUID) -> None:
    """Push the school's current lists. One sync per school at a time; each reads the
    lists when its turn comes, so the last one always sends the latest state."""
    lock = _sync_locks.setdefault(org_id, asyncio.Lock())
    async with lock:
        async with db.sessionmaker()() as s:
            payload = await sync_payload(s, org_id)
        await push_to_sinkhole(payload)


async def sync_sinkhole(ctx: Ctx) -> None:
    """After a change (committed): push the school's full state (also: `smp dns-sync`)."""
    try:
        await sync_org(ctx.org_id)
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


def _jobs(request: Request) -> imports.ImportJobs:
    return request.app.state.registry.get("dns").imports


def _job_out(job: imports.ImportJob) -> ImportJobOut:
    return ImportJobOut(
        id=job.id, state=job.state, progress=round(job.progress, 3), file_bytes=job.file_bytes,
        progress_current=job.progress_current, progress_total=job.progress_total,
        new=len(job.new) if job.added is None else job.added, existing=len(job.existing), skipped=len(job.skipped),
        added=job.added, sync_ok=job.sync_ok, error=ImportErrorOut(**job.error) if job.error else None,
    )


def _own_job(request: Request, ctx: Ctx, job_id: str) -> imports.ImportJob:
    job = _jobs(request).get(job_id, ctx.org_id, ctx.principal.id)
    if job is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "import not found", entity="import")
    return job


@router.post("/blocklist/imports", response_model=ImportJobOut, status_code=202)
async def start_import(request: Request, ctx: Ctx = Depends(require(WRITE))):
    """Upload a hosts, domain-only or Adblock list (raw body). Checking runs in the
    background; poll the job. One import per person (see imports.py)."""
    content = await read_limited(request, imports.MAX_IMPORT_BYTES)
    job = imports.ImportJob(org_id=ctx.org_id, owner_id=ctx.principal.id, owner_name=ctx.principal.name,
                            file_bytes=len(content))
    match _jobs(request).add(job):
        case "dns.import_in_progress":
            raise AppError(status.HTTP_429_TOO_MANY_REQUESTS, "dns.import_in_progress",
                           "your previous import is still being checked or saved; wait until it's finished")
        case "dns.too_many_imports":
            raise AppError(status.HTTP_429_TOO_MANY_REQUESTS, "dns.too_many_imports",
                           "too many imports at once in this school; try again in a few minutes",
                           max=imports.MAX_JOBS_PER_ORG)
    _jobs(request).run(imports.check(job, content))
    return _job_out(job)


@router.get("/blocklist/imports/{job_id}", response_model=ImportJobOut)
async def import_status(job_id: str, request: Request, ctx: Ctx = Depends(require(WRITE))):
    return _job_out(_own_job(request, ctx, job_id))


@router.get("/blocklist/imports/{job_id}/domains", response_model=Page[str])
async def import_domains(
    job_id: str,
    request: Request,
    ctx: Ctx = Depends(require(WRITE)),
    kind: Literal["new", "existing", "skipped"] = "new",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[str]:
    """One page of the preview: what the import adds, what's already listed, what's skipped."""
    job = _own_job(request, ctx, job_id)
    domains = {"new": job.new, "existing": job.existing, "skipped": job.skipped}[kind]
    return Page(items=domains[offset:offset + limit], total=len(domains))


@router.post("/blocklist/imports/{job_id}/confirm", response_model=ImportJobOut, status_code=202)
async def confirm_import(job_id: str, request: Request, ctx: Ctx = Depends(require(WRITE))):
    """Add the new domains (in the background; poll the job)."""
    job = _own_job(request, ctx, job_id)
    if job.state != "ready":
        raise AppError(status.HTTP_409_CONFLICT, "dns.import_not_ready", f"the import is {job.state}", state=job.state)
    if not job.new:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "dns.no_valid_domains", "no new domains to import")
    p = ctx.principal
    actor = imports.Actor(kind=p.kind, id=str(p.id), name=p.name, ip=client_ip(request),
                          user_agent=(request.headers.get("user-agent") or "")[:400] or None)
    job.state, job.progress = "saving", 0.0
    _jobs(request).run(imports.save(job, actor, request.app.state.bus, _sync_for_jobs))
    return _job_out(job)


async def _sync_for_jobs(org_id: uuid.UUID) -> None:
    await sync_org(org_id)  # looked up at call time (tests replace sync_org)


@router.delete("/blocklist/imports/{job_id}", status_code=204)
async def cancel_import(job_id: str, request: Request, ctx: Ctx = Depends(require(WRITE))):
    job = _own_job(request, ctx, job_id)
    if job.state in ("saving", "syncing"):
        raise AppError(status.HTTP_409_CONFLICT, "dns.import_running", "the import is being saved; it can't be stopped now")
    _jobs(request).remove(job.id)


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
    labels = d.split(".")
    candidates = [".".join(labels[i:]) for i in range(len(labels))]  # the domain and its parents
    block_rows = await ctx.db.execute(ctx.select(DnsBlocklistEntry).where(DnsBlocklistEntry.domain.in_(candidates)))
    allow, block = by_domain(await _allowlist(ctx)), by_domain(block_rows.scalars())
    entry = blocked_by(d, allow, block)
    covering = match(d, allow)
    return {
        "domain": d,
        "blocked": entry is not None,
        "blocked_by": entry.domain if entry else None,
        "builtin": isinstance(entry, BuiltinBlock),
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
        builtin_blocklist=list(BUILTIN_BLOCKLIST),
        sinkhole_url=settings.dns_sinkhole_url,
        sync_configured=sync_configured,
        block_page_language=(await _dns_settings(ctx.db, ctx.org_id)).block_page_language,
    )
