# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""rejected join requests are remembered (by fingerprint and MAC)

Revision ID: 0013_join_rejections
Revises: 0012_agent_reported_ip
Create Date: 2026-11-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_join_rejections"
down_revision: str | Sequence[str] | None = "0012_agent_reported_ip"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_join_requests", sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_agent_join_requests_org_mac", "agent_join_requests", ["org_id", "mac"])


def downgrade() -> None:
    op.drop_index("ix_agent_join_requests_org_mac", table_name="agent_join_requests")
    op.drop_column("agent_join_requests", "rejected_at")
