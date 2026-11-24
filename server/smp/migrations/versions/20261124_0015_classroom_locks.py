# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""classroom: screen locks per computer, the school's lock screen logo

Revision ID: 0015_classroom_locks
Revises: 0014_software
Create Date: 2026-11-24 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_classroom_locks"
down_revision: str | Sequence[str] | None = "0014_software"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common() -> list:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    ]


def upgrade() -> None:
    op.create_table(
        "classroom_locks",
        *_common(),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("locked_by", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("device_id"),
    )
    op.create_index("ix_classroom_locks_org_id", "classroom_locks", ["org_id"])
    op.create_index("ix_classroom_locks_room_id", "classroom_locks", ["room_id"])
    op.create_table(
        "classroom_lock_logos",
        *_common(),
        sa.Column("png", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.UniqueConstraint("org_id"),
    )
    op.create_index("ix_classroom_lock_logos_org_id", "classroom_lock_logos", ["org_id"])


def downgrade() -> None:
    op.drop_table("classroom_lock_logos")
    op.drop_table("classroom_locks")
