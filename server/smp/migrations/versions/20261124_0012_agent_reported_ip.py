# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""agents report their own IP address (devices behind a NAT)

devices.last_ip becomes the address the agent reports; devices.observed_ip keeps the
address SMP sees. Join requests keep the seen address in `ip` and get `reported_ip`.

Revision ID: 0012_agent_reported_ip
Revises: 0011_user_preferences
Create Date: 2026-11-24 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_agent_reported_ip"
down_revision: str | Sequence[str] | None = "0011_user_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("observed_ip", sa.String(45), nullable=True))
    # Until each agent reports again, what we had was the seen address.
    op.execute("UPDATE devices SET observed_ip = last_ip WHERE agent_id IS NOT NULL")
    op.add_column("agent_join_requests", sa.Column("reported_ip", sa.String(45), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_join_requests", "reported_ip")
    op.drop_column("devices", "observed_ip")
