# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""agent: device identity keys, room mode, logged-in user; join requests

Revision ID: 0009_agent
Revises: 0008_dns_allowlist
Create Date: 2026-11-22 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0009_agent'
down_revision: str | Sequence[str] | None = '0008_dns_allowlist'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("agent_public_key", sa.Text(), nullable=True))
    op.add_column("devices", sa.Column("room_mode", sa.String(16), nullable=False, server_default="fixed"))
    op.add_column("devices", sa.Column("logged_in_user", sa.String(256), nullable=True))
    op.create_table(
        "agent_join_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("mac", sa.String(17), nullable=True),
        sa.Column("os_name", sa.String(64), nullable=True),
        sa.Column("os_version", sa.String(64), nullable=True),
        sa.Column("agent_version", sa.String(32), nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "fingerprint"),
    )


def downgrade() -> None:
    op.drop_table("agent_join_requests")
    op.drop_column("devices", "logged_in_user")
    op.drop_column("devices", "room_mode")
    op.drop_column("devices", "agent_public_key")
