import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, status
from fastapi.responses import RedirectResponse
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from smp.core.context import Ctx, public_ctx, require
from smp.core.db import utcnow
from smp.core.errors import AppError
from smp.modules.shortlinks import ShortlinksSettings
from smp.modules.shortlinks.models import ShortLink

router = APIRouter()  # mounted at /api/v1/shortlinks
public_router = APIRouter()  # mounted at /

CREATE = "shortlinks.links.create"
MANAGE_ALL = "shortlinks.links.manage_all"
_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"  # no 0/o/1/l: codes get read aloud in class


class LinkIn(BaseModel):
    target_url: AnyHttpUrl  # http/https only; rejects javascript: and friends
    code: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{3,64}$")
    title: str = Field("", max_length=200)
    expires_at: datetime | None = None


class LinkUpdate(BaseModel):
    target_url: AnyHttpUrl | None = None
    title: str | None = Field(None, max_length=200)
    expires_at: datetime | None = None
    disabled: bool | None = None


class LinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    target_url: str
    title: str
    owner_id: uuid.UUID | None
    expires_at: datetime | None
    disabled: bool
    clicks: int
    created_at: datetime


async def _own_link(ctx: Ctx, link_id: uuid.UUID) -> ShortLink:
    link = await ctx.get(ShortLink, link_id)
    if link.owner_id != ctx.principal.id and not ctx.principal.can(MANAGE_ALL):
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "ShortLink not found", entity="ShortLink")
    return link


@router.get("/links", response_model=list[LinkOut])
async def list_links(all: bool = False, ctx: Ctx = Depends(require(CREATE))):
    stmt = ctx.select(ShortLink).order_by(ShortLink.created_at.desc())
    if not (all and ctx.principal.can(MANAGE_ALL)):
        stmt = stmt.where(ShortLink.owner_id == ctx.principal.id)
    return list((await ctx.db.execute(stmt)).scalars())


@router.post("/links", response_model=LinkOut, status_code=201)
async def create_link(body: LinkIn, ctx: Ctx = Depends(require(CREATE))):
    settings: ShortlinksSettings = await ctx.settings("shortlinks")  # type: ignore[assignment]
    owned = await ctx.db.scalar(
        select(func.count()).where(ShortLink.org_id == ctx.org_id, ShortLink.owner_id == ctx.principal.id)
    )
    if (owned or 0) >= settings.max_links_per_user:
        raise AppError(
            status.HTTP_409_CONFLICT, "shortlinks.limit_reached", "link limit reached", max=settings.max_links_per_user
        )

    expires_at = body.expires_at
    if expires_at is None and settings.default_expiry_days:
        expires_at = utcnow() + timedelta(days=settings.default_expiry_days)

    code = body.code or "".join(secrets.choice(_ALPHABET) for _ in range(6))
    link = ShortLink(
        org_id=ctx.org_id,
        code=code,
        target_url=str(body.target_url),
        title=body.title,
        owner_id=ctx.principal.id,
        expires_at=expires_at,
    )
    ctx.db.add(link)
    await ctx.db.flush()  # duplicate code -> IntegrityError -> 409
    ctx.audit("shortlinks.link.create", link, {"code": code, "target_url": link.target_url})
    ctx.emit("shortlinks.link.created", {"id": str(link.id), "code": code})
    await ctx.commit()
    return link


@router.patch("/links/{link_id}", response_model=LinkOut)
async def update_link(link_id: uuid.UUID, body: LinkUpdate, ctx: Ctx = Depends(require(CREATE))):
    link = await _own_link(ctx, link_id)
    changes = body.model_dump(mode="json", exclude_unset=True)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(link, k, str(v) if k == "target_url" and v is not None else v)
    ctx.audit("shortlinks.link.update", link, changes)
    await ctx.commit()
    return link


@router.delete("/links/{link_id}", status_code=204)
async def delete_link(link_id: uuid.UUID, ctx: Ctx = Depends(require(CREATE))):
    link = await _own_link(ctx, link_id)
    ctx.audit("shortlinks.link.delete", link, {"code": link.code, "target_url": link.target_url})
    ctx.emit("shortlinks.link.deleted", {"id": str(link.id), "code": link.code})
    await ctx.db.delete(link)
    await ctx.commit()


@public_router.get("/s/{code}", include_in_schema=False)
async def follow(code: str, ctx: Ctx = Depends(public_ctx)):
    now = utcnow()
    target = await ctx.db.scalar(
        update(ShortLink)
        .where(
            ShortLink.org_id == ctx.org_id,
            ShortLink.code == code,
            ShortLink.disabled.is_(False),
            (ShortLink.expires_at.is_(None)) | (ShortLink.expires_at > now),
        )
        .values(clicks=ShortLink.clicks + 1)
        .returning(ShortLink.target_url)
    )
    if target is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "shortlinks.not_found_or_expired", "link not found or expired")
    await ctx.commit()
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)
