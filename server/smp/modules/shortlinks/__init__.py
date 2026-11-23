# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Link shortener. Also the reference module: copy its layout when starting a new one.

    shortlinks/
      __init__.py  manifest (this file): permissions, settings, ui routes, events
      models.py    SQLAlchemy tables, prefixed with the module name
      api.py       FastAPI routers (authenticated + public)
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from smp.core.module import EventSubject, Module, Permission, UiRoute, Widget


class ShortlinksSettings(BaseModel):
    default_expiry_days: int | None = Field(None, ge=1, description="Applied when the creator sets no expiry")
    max_links_per_user: int = Field(200, ge=1)


class ShortlinksModule(Module):
    name = "shortlinks"
    title = "Short links"
    description = "Short links with expiry and per-teacher ownership"

    permissions = (
        Permission("shortlinks.links.create", "Create and manage own short links", ("teacher",)),
        Permission("shortlinks.links.manage_all", "Manage everyone's short links"),
    )
    ui_routes = (UiRoute("/shortlinks", "Short links", "🔗", "shortlinks.links.create", "services"),)
    events = (
        EventSubject("shortlinks.link.created", "A short link was created"),
        EventSubject("shortlinks.link.deleted", "A short link was deleted"),
    )
    settings_model = ShortlinksSettings

    async def dashboard(self, ctx) -> list[Widget]:
        if not ctx.principal.can("shortlinks.links.create"):
            return []
        from sqlalchemy import func, select

        from smp.modules.shortlinks.models import ShortLink

        n, clicks = (
            await ctx.db.execute(
                select(func.count(), func.coalesce(func.sum(ShortLink.clicks), 0)).where(
                    ShortLink.org_id == ctx.org_id, ShortLink.owner_id == ctx.principal.id
                )
            )
        ).one()
        return [Widget(id="shortlinks.mine", title="My short links", value=n,
                       hint=f"{clicks} click" + ("" if clicks == 1 else "s"), data={"clicks": int(clicks)},
                       icon="🔗", link="/shortlinks", order=70)]

    def router(self) -> APIRouter:
        from smp.modules.shortlinks.api import router

        return router

    def public_router(self) -> APIRouter:
        from smp.modules.shortlinks.api import public_router

        return public_router


# Import models at package import time so Alembic sees the tables.
from smp.modules.shortlinks import models  # noqa: E402, F401
