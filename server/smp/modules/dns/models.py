# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from smp.core.db import Base, OrgScoped, Timestamps, UUIDPk


class DnsBlocklistEntry(UUIDPk, Timestamps, OrgScoped, Base):
    __tablename__ = "dns_blocklist_entries"
    __table_args__ = (UniqueConstraint("org_id", "domain"),)

    domain: Mapped[str] = mapped_column(String(253))
    include_subdomains: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    added_by: Mapped[str] = mapped_column(String(200))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DnsAllowlistEntry(UUIDPk, Timestamps, OrgScoped, Base):
    """Domains that are never blocked. Checked before the blocklist (see
    domains.py and the Go schoolfilter's Match), on top of the built-in public
    suffixes (com, co.uk, hu, ...) which can't be blocked either."""

    __tablename__ = "dns_allowlist_entries"
    __table_args__ = (UniqueConstraint("org_id", "domain"),)

    domain: Mapped[str] = mapped_column(String(253))
    include_subdomains: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    added_by: Mapped[str] = mapped_column(String(200))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
