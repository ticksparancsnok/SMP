import ipaddress
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from smp.core.config import get_settings
from smp.core.context import Ctx, require
from smp.modules.dns.models import DnsBlocklistEntry

router = APIRouter()

READ = "dns.blocklist.read"
WRITE = "dns.blocklist.write"


class EntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1, max_length=253)
    include_subdomains: bool = True
    reason: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    domain: str
    includeSubdomains: bool
    reason: str
    addedBy: str
    confidence: float | None
    addedAt: datetime


class HostsPreviewOut(BaseModel):
    domains: list[str]
    unique_count: int


class SyncStatusOut(BaseModel):
    entries: int
    sinkhole_url: str
    sync_configured: bool


def normalize_domain(domain: str) -> str | None:
    domain = domain.lower().strip().rstrip(".")
    if not domain or any(c.isspace() or c in "/\\" for c in domain):
        return None
    if domain in {"localhost", "localhost.localdomain", "broadcasthost"}:
        return None
    try:
        ipaddress.ip_address(domain)
        return None
    except ValueError:
        return domain


def parse_hosts(content: bytes) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw_line in content.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        try:
            ip = ipaddress.ip_address(tokens[0])
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            if str(ip) not in {"0.0.0.0", "127.0.0.1"}:
                continue
        for token in tokens[1:]:
            domain = normalize_domain(token)
            if domain and domain not in seen:
                seen.add(domain)
                found.append(domain)
    return found


def row_to_out(row: DnsBlocklistEntry) -> EntryOut:
    return EntryOut(
        id=str(row.id),
        domain=row.domain,
        includeSubdomains=row.include_subdomains,
        reason=row.reason,
        addedBy=row.added_by,
        confidence=row.confidence,
        addedAt=row.added_at,
    )


async def sync_sinkhole(ctx: Ctx) -> None:
    rows = list(
        (
            await ctx.db.execute(
                select(DnsBlocklistEntry).where(DnsBlocklistEntry.org_id == ctx.org_id).order_by(DnsBlocklistEntry.domain)
            )
        ).scalars()
    )
    payload = {
        "entries": [
            {
                "domain": row.domain,
                "includeSubdomains": row.include_subdomains,
                "reason": row.reason,
                "addedBy": row.added_by,
                "confidence": row.confidence,
                "addedAt": row.added_at.isoformat(),
            }
            for row in rows
        ]
    }
    settings = get_settings()
    headers = {}
    if settings.dns_sync_token:
        headers["Authorization"] = f"Bearer {settings.dns_sync_token}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            response = await client.post(
                settings.dns_sinkhole_url.rstrip("/") + "/api/blocklist/sync",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "blocklist was saved, but the DNS sinkhole could not be synchronized",
            ) from exc


@router.get("/blocklist", response_model=list[EntryOut])
async def list_blocklist(ctx: Ctx = Depends(require(READ))) -> list[EntryOut]:
    rows = (
        await ctx.db.execute(
            select(DnsBlocklistEntry).where(DnsBlocklistEntry.org_id == ctx.org_id).order_by(DnsBlocklistEntry.domain)
        )
    ).scalars()
    return [row_to_out(row) for row in rows]


@router.post("/blocklist", response_model=EntryOut, status_code=201)
async def create_entry(body: EntryIn, ctx: Ctx = Depends(require(WRITE))):
    domain = normalize_domain(body.domain)
    if domain is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid domain")
    now = datetime.now(timezone.utc)
    row = DnsBlocklistEntry(
        org_id=ctx.org_id,
        domain=domain,
        include_subdomains=body.include_subdomains,
        reason=body.reason,
        added_by=ctx.principal.name,
        confidence=body.confidence,
        added_at=now,
    )
    ctx.db.add(row)
    try:
        await ctx.db.flush()
    except Exception as exc:
        await ctx.db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "domain already exists") from exc
    ctx.audit("dns.blocklist.create", row, {"domain": domain, "addedBy": ctx.principal.name})
    ctx.emit("dns.blocklist.updated", {"domain": domain})
    await ctx.commit()
    await sync_sinkhole(ctx)
    return row_to_out(row)


@router.delete("/blocklist/{entry_id}", status_code=204)
async def delete_entry(entry_id: str, ctx: Ctx = Depends(require(WRITE))):
    from uuid import UUID

    try:
        parsed = UUID(entry_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "blocklist entry not found") from exc
    row = await ctx.get(DnsBlocklistEntry, parsed)
    ctx.audit("dns.blocklist.delete", row, {"domain": row.domain})
    ctx.emit("dns.blocklist.updated", {"domain": row.domain})
    await ctx.db.delete(row)
    await ctx.commit()
    await sync_sinkhole(ctx)


@router.post("/blocklist/import-preview", response_model=HostsPreviewOut)
async def preview_import(request: Request, ctx: Ctx = Depends(require(WRITE))):
    domains = parse_hosts(await request.body())
    return HostsPreviewOut(domains=domains, unique_count=len(domains))


@router.post("/blocklist/import", response_model=HostsPreviewOut, status_code=201)
async def import_hosts(request: Request, ctx: Ctx = Depends(require(WRITE))):
    domains = parse_hosts(await request.body())
    if not domains:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no valid domains found")
    now = datetime.now(timezone.utc)
    imported = 0
    for domain in domains:
        existing = await ctx.db.scalar(
            select(DnsBlocklistEntry).where(
                DnsBlocklistEntry.org_id == ctx.org_id,
                DnsBlocklistEntry.domain == domain,
            )
        )
        if existing:
            continue
        ctx.db.add(
            DnsBlocklistEntry(
                org_id=ctx.org_id,
                domain=domain,
                include_subdomains=True,
                reason="imported",
                added_by=f"imported by: {ctx.principal.name}",
                confidence=None,
                added_at=now,
            )
        )
        imported += 1
    ctx.audit(
        "dns.blocklist.import",
        target_type="dns_blocklist",
        details={"imported": imported, "addedBy": f"imported by: {ctx.principal.name}"},
    )
    ctx.emit("dns.blocklist.updated", {"imported": imported})
    await ctx.commit()
    await sync_sinkhole(ctx)
    return HostsPreviewOut(domains=domains, unique_count=imported)


@router.get("/status", response_model=SyncStatusOut)
async def status(ctx: Ctx = Depends(require(READ))):
    count = await ctx.db.scalar(
        select(func.count()).where(DnsBlocklistEntry.org_id == ctx.org_id)
    )
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
            sync_configured = response.status_code < 500
    except httpx.HTTPError:
        sync_configured = False
    return SyncStatusOut(
        entries=count or 0,
        sinkhole_url=settings.dns_sinkhole_url,
        sync_configured=sync_configured,
    )
