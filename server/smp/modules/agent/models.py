from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from smp.core.db import Base, OrgScoped, Timestamps, UUIDPk


class AgentJoinRequest(UUIDPk, Timestamps, OrgScoped, Base):
    """A machine asking to be managed. An admin compares the fingerprint shown on the
    machine with the one listed here and approves (-> Device) or rejects it."""

    __tablename__ = "agent_join_requests"
    __table_args__ = (UniqueConstraint("org_id", "fingerprint"),)

    fingerprint: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[str] = mapped_column(Text)
    hostname: Mapped[str] = mapped_column(String(255))
    mac: Mapped[str | None] = mapped_column(String(17))
    os_name: Mapped[str | None] = mapped_column(String(64))
    os_version: Mapped[str | None] = mapped_column(String(64))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    ip: Mapped[str | None] = mapped_column(String(45))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
