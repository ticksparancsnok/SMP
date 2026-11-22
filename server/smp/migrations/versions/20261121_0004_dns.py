"""DNS sinkhole blocklist entries

Revision ID: 0004_dns
Revises: 0003_otp
Create Date: 2026-11-21 22:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_dns"
down_revision: str | Sequence[str] | None = "0003_otp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dns_blocklist_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("include_subdomains", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("added_by", sa.String(length=200), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "domain"),
    )
    op.create_index(
        "ix_dns_blocklist_entries_org_domain",
        "dns_blocklist_entries",
        ["org_id", "domain"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_dns_blocklist_entries_org_domain", table_name="dns_blocklist_entries")
    op.drop_table("dns_blocklist_entries")
