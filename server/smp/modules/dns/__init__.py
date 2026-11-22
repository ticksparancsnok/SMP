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
    title = "DNS tartalomszűrés"
    description = "Host blokklista, DNS sinkhole szinkronizáció és importálás"
    permissions = (
        Permission("dns.blocklist.read", "DNS blokklista megtekintése", ("admin", "teacher")),
        Permission("dns.blocklist.write", "DNS blokklista módosítása", ("admin", "teacher")),
        Permission("dns.review.manage", "DNS AI felülvizsgálati sor kezelése", ("admin", "teacher")),
    )
    ui_routes = (UiRoute("/dns", "Oldal blokkoló", "🌐", "dns.blocklist.read", "services"),)
    policy_model = DnsPolicy
    events = (
        EventSubject("dns.blocklist.updated", "A DNS blokklista megváltozott"),
        EventSubject("dns.policy.sync", "A DNS policy szinkronizálásra vár"),
        EventSubject("dns.domain.unclassified", "Egy ismeretlen domain besorolásra vár"),
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
                title="Blokkolt domainek",
                value=count or 0,
                hint="DNS blokklista bejegyzések",
                icon="🌐",
                link="/dns",
                order=60,
            )
        ]


# Import models at package import time so Alembic sees the DNS tables.
from smp.modules.dns import models  # noqa: E402, F401
