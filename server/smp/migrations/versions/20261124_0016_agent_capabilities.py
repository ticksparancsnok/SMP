# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""devices: what each PC's agent can do (reported in its heartbeats)

Revision ID: 0016_agent_capabilities
Revises: 0015_classroom_locks
Create Date: 2026-11-24 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_agent_capabilities"
down_revision: str | Sequence[str] | None = "0015_classroom_locks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL: an agent from before 1.0.4, which doesn't report its capabilities.
    op.add_column("devices", sa.Column("agent_capabilities", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "agent_capabilities")
