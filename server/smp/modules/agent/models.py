# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from smp.core.db import Base, OrgScoped, Timestamps, UUIDPk


class AgentJoinRequest(UUIDPk, Timestamps, OrgScoped, Base):
    """A machine asking to be managed. An admin compares the fingerprint shown on the
    machine with the one listed here and approves (-> Device) or rejects it."""

    __tablename__ = "agent_join_requests"
    __table_args__ = (UniqueConstraint("org_id", "fingerprint"), Index("ix_agent_join_requests_org_mac", "org_id", "mac"))

    fingerprint: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[str] = mapped_column(Text)
    hostname: Mapped[str] = mapped_column(String(255))
    mac: Mapped[str | None] = mapped_column(String(17))
    os_name: Mapped[str | None] = mapped_column(String(64))
    os_version: Mapped[str | None] = mapped_column(String(64))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    # ip: the address SMP sees the request come from (the per-address limit on pending
    # requests uses it: it can't be made up). reported_ip: the machine's own address, as
    # its agent says (differs behind a NAT); None from agents before 1.0.3.
    ip: Mapped[str | None] = mapped_column(String(45))
    reported_ip: Mapped[str | None] = mapped_column(String(45))
    # Set when an admin rejected it: then it's a memory (by fingerprint and MAC) that
    # stops the machine from asking again, until it expires or someone allows it again.
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
