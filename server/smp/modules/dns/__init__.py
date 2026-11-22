# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""DNS sinkhole control-plane module."""

from pydantic import BaseModel

from smp.core.module import EventSubject, Module, Permission, UiRoute, Widget


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
    events = (
        EventSubject("dns.blocklist.updated", "The DNS blocklist changed"),
        EventSubject("dns.allowlist.updated", "The DNS allowlist changed"),
        EventSubject("dns.policy.sync", "A DNS policy is waiting to be synchronized"),
        EventSubject("dns.domain.unclassified", "An unknown domain is waiting to be classified"),
    )

    def router(self):
        from smp.modules.dns.api import router

        return router

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
