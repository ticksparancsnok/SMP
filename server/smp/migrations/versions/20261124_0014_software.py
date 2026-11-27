# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""software: uploads, packages, assignments, per-computer states

Revision ID: 0014_software
Revises: 0013_join_rejections
Create Date: 2026-11-24 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_software"
down_revision: str | Sequence[str] | None = "0013_join_rejections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "software_uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_software_uploads_org_id", "software_uploads", ["org_id"])
    op.create_index("ix_software_uploads_sha256", "software_uploads", ["sha256"])

    op.create_table(
        "software_packages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("publisher", sa.String(200), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("install_args", sa.String(2000), nullable=False),
        sa.Column("uninstall_command", sa.String(2000), nullable=False),
        sa.Column("detection", postgresql.JSONB(), nullable=False),
        sa.Column("reboot", sa.String(16), nullable=False),
        sa.Column("success_codes", postgresql.JSONB(), nullable=False),
        sa.Column("timeout_s", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["upload_id"], ["software_uploads.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_software_packages_org_id", "software_packages", ["org_id"])

    op.create_table(
        "software_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("package_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(8), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=True),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["package_id"], ["software_packages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id", "room_id", "device_id", postgresql_nulls_not_distinct=True),
        sa.CheckConstraint(
            "(target_type = 'org' AND room_id IS NULL AND device_id IS NULL)"
            " OR (target_type = 'room' AND room_id IS NOT NULL AND device_id IS NULL)"
            " OR (target_type = 'device' AND device_id IS NOT NULL AND room_id IS NULL)",
            name="target",
        ),
    )
    op.create_index("ix_software_assignments_org_id", "software_assignments", ["org_id"])
    op.create_index("ix_software_assignments_package_id", "software_assignments", ["package_id"])
    op.create_index("ix_software_assignments_room_id", "software_assignments", ["room_id"])
    op.create_index("ix_software_assignments_device_id", "software_assignments", ["device_id"])

    op.create_table(
        "software_device_states",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("package_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("exit_code", sa.BigInteger(), nullable=True),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["package_id"], ["software_packages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("device_id", "package_id"),
    )
    op.create_index("ix_software_device_states_org_id", "software_device_states", ["org_id"])
    op.create_index("ix_software_device_states_package_id", "software_device_states", ["package_id"])


def downgrade() -> None:
    op.drop_table("software_device_states")
    op.drop_table("software_assignments")
    op.drop_table("software_packages")
    op.drop_table("software_uploads")
