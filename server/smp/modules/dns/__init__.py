# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""DNS sinkhole control-plane module."""

import logging
from typing import Literal

from pydantic import BaseModel, Field

from smp.core.module import EventSubject, Module, Permission, UiRoute, Widget

log = logging.getLogger(__name__)


class DnsSettings(BaseModel):
    block_page_language: Literal["hu", "en"] = Field(
        "hu",
        description="Language of the page shown for a blocked site (visitors can still switch it there)",
    )


class DnsPolicy(BaseModel):
    """Policy schema exposed to the shared policy framework."""

    blocked_domains: list[str] = []
    blocked_categories: list[str] = []
    include_subdomains: bool = True
    whitelist_mode: bool = False
    force_safe_search: bool = False
    sinkhole_ipv4: str = "127.0.0.1"


class DnsModule(Module):
    name = "dns"
    title = "DNS content filtering"
    description = "Hosts blocklist and allowlist, DNS sinkhole synchronization and import"
    permissions = (
        Permission("dns.blocklist.read", "View the DNS blocklist and allowlist", ("admin", "teacher")),
        Permission("dns.blocklist.write", "Edit the DNS blocklist and allowlist", ("admin", "teacher")),
        Permission("dns.review.manage", "Manage the DNS AI review queue", ("admin", "teacher")),
    )
    ui_routes = (UiRoute("/dns", "Site blocker", "🌐", "dns.blocklist.read", "services"),)
    policy_model = DnsPolicy
    settings_model = DnsSettings
    events = (
        EventSubject("dns.blocklist.updated", "The DNS blocklist changed"),
        EventSubject("dns.allowlist.updated", "The DNS allowlist changed"),
        EventSubject("dns.policy.sync", "A DNS policy is waiting to be synchronized"),
        EventSubject("dns.domain.unclassified", "An unknown domain is waiting to be classified"),
    )

    def __init__(self) -> None:
        from smp.modules.dns.imports import ImportJobs

        self.imports = ImportJobs()  # hosts-file imports in progress (imports.py)

    def router(self):
        from smp.modules.dns.api import router

        return router

    def subscribe(self, bus) -> None:
        async def settings_changed(event) -> None:
            # The block page language travels with the lists: send them again.
            if (event.payload or {}).get("module") != self.name or event.org_id is None:
                return
            from smp.modules.dns import api

            try:
                await api.sync_org(event.org_id)
            except Exception as e:  # noqa: BLE001 - saved anyway; the next sync carries it
                log.warning("DNS settings changed, but the sinkhole couldn't be updated yet: %s", e)

        bus.subscribe("core.settings.changed", settings_changed)

    async def dashboard(self, ctx) -> list[Widget]:
        if not ctx.principal.can("dns.blocklist.read"):
            return []
        from sqlalchemy import func, select

        from smp.modules.dns.models import DnsBlocklistEntry

        count = await ctx.db.scalar(
            select(func.count()).where(DnsBlocklistEntry.org_id == ctx.org_id)
        )
        return [
            Widget(
                id="dns.blocklist",
                title="Blocked domains",
                value=count or 0,
                hint="DNS blocklist entries",
                icon="🌐",
                link="/dns",
                order=60,
            )
        ]


# Import models at package import time so Alembic sees the DNS tables.
from smp.modules.dns import models  # noqa: E402, F401
