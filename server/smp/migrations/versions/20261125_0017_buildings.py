# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""buildings: a real Building entity and scoped roles (manager, unit_manager)

rooms.building (free text) becomes buildings + rooms.building_id: one building per
distinct trimmed, non-empty text per organization. Roles can be assigned for a building,
groups can belong to buildings, policies / schedules can be owned by one, audit entries
and software assignments can point at one, and agent join requests carry the building the
installer typed.

Revision ID: 0017_buildings
Revises: 0016_agent_capabilities
Create Date: 2026-11-25 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_buildings"
down_revision: str | Sequence[str] | None = "0016_agent_capabilities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None



def _trim(column: str) -> str:
    """The old free-text value trimmed of spaces, tabs and line breaks."""
    return rf"btrim({column}, E' \t\r\n')"


_TARGET_OLD = (
    "(target_type = 'org' AND room_id IS NULL AND device_id IS NULL)"
    " OR (target_type = 'room' AND room_id IS NOT NULL AND device_id IS NULL)"
    " OR (target_type = 'device' AND device_id IS NOT NULL AND room_id IS NULL)"
)
_TARGET_NEW = (
    "(target_type = 'org' AND building_id IS NULL AND room_id IS NULL AND device_id IS NULL)"
    " OR (target_type = 'building' AND building_id IS NOT NULL AND room_id IS NULL AND device_id IS NULL)"
    " OR (target_type = 'room' AND room_id IS NOT NULL AND building_id IS NULL AND device_id IS NULL)"
    " OR (target_type = 'device' AND device_id IS NOT NULL AND building_id IS NULL AND room_id IS NULL)"
)


def _add_building_fk(table: str, ondelete: str, *, index: bool) -> None:
    """A nullable building_id column with its foreign key (and index)."""
    op.add_column(table, sa.Column("building_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f(f"fk_{table}_building_id_buildings"), table, "buildings", ["building_id"], ["id"], ondelete=ondelete
    )
    if index:
        op.create_index(op.f(f"ix_{table}_building_id"), table, ["building_id"])


def _drop_building_fk(table: str, *, index: bool) -> None:
    if index:
        op.drop_index(op.f(f"ix_{table}_building_id"), table_name=table)
    op.drop_constraint(op.f(f"fk_{table}_building_id_buildings"), table, type_="foreignkey")
    op.drop_column(table, "building_id")


def upgrade() -> None:
    # ---- buildings, and the rooms' free-text building turned into them
    op.create_table(
        "buildings",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name=op.f("fk_buildings_org_id_organizations"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_buildings")),
        sa.UniqueConstraint("org_id", "name", name=op.f("uq_buildings_org_id_name")),
    )
    op.create_index(op.f("ix_buildings_org_id"), "buildings", ["org_id"])

    op.execute(
        f"""
        INSERT INTO buildings (id, org_id, name, description, created_at, updated_at)
        SELECT gen_random_uuid(), org_id, {_trim("building")}, '', now(), now()
        FROM rooms
        WHERE {_trim("building")} <> ''
        GROUP BY org_id, {_trim("building")}
        """
    )
    _add_building_fk("rooms", "SET NULL", index=True)
    op.execute(
        f"""
        UPDATE rooms SET building_id = b.id
        FROM buildings b
        WHERE b.org_id = rooms.org_id AND b.name = {_trim("rooms.building")}
        """
    )
    op.drop_column("rooms", "building")

    # ---- role assignments: org, one building or one room
    _add_building_fk("role_assignments", "CASCADE", index=True)
    op.create_check_constraint(
        op.f("ck_role_assignments_one_scope"), "role_assignments", "room_id IS NULL OR building_id IS NULL"
    )
    op.drop_constraint(op.f("uq_role_assignments_user_id_role_room_id"), "role_assignments", type_="unique")
    op.create_unique_constraint(
        op.f("uq_role_assignments_user_id_role_room_id_building_id"),
        "role_assignments",
        ["user_id", "role", "room_id", "building_id"],
        postgresql_nulls_not_distinct=True,
    )

    # ---- groups in buildings (none = school-wide)
    op.create_table(
        "group_buildings",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("building_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["building_id"], ["buildings.id"], name=op.f("fk_group_buildings_building_id_buildings"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name=op.f("fk_group_buildings_group_id_groups"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name=op.f("fk_group_buildings_org_id_organizations"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("group_id", "building_id", name=op.f("pk_group_buildings")),
    )
    op.create_index(op.f("ix_group_buildings_building_id"), "group_buildings", ["building_id"])
    op.create_index(op.f("ix_group_buildings_org_id"), "group_buildings", ["org_id"])

    # ---- policies and schedules: an owner building (NULL = school-wide)
    _add_building_fk("policies", "RESTRICT", index=True)
    op.drop_constraint(op.f("uq_policies_org_id_kind_name"), "policies", type_="unique")
    op.create_unique_constraint(
        op.f("uq_policies_org_id_building_id_kind_name"),
        "policies",
        ["org_id", "building_id", "kind", "name"],
        postgresql_nulls_not_distinct=True,
    )
    _add_building_fk("schedules", "RESTRICT", index=True)
    op.drop_constraint(op.f("uq_schedules_org_id_name"), "schedules", type_="unique")
    op.create_unique_constraint(
        op.f("uq_schedules_org_id_building_id_name"),
        "schedules",
        ["org_id", "building_id", "name"],
        postgresql_nulls_not_distinct=True,
    )

    # ---- audit log: which buildings an entry is about (NULL = org-level). No foreign
    # key (history outlives a building) and no UPDATE (the table is append-only).
    op.add_column("audit_log", sa.Column("building_ids", postgresql.ARRAY(sa.Uuid()), nullable=True))
    op.create_index("ix_audit_log_building_ids", "audit_log", ["building_ids"], postgresql_using="gin")

    # ---- software assignments: school > building > room > computer
    _add_building_fk("software_assignments", "CASCADE", index=True)
    op.drop_constraint(op.f("ck_software_assignments_target"), "software_assignments", type_="check")
    op.drop_constraint(op.f("uq_software_assignments_package_id_room_id_device_id"), "software_assignments", type_="unique")
    op.create_check_constraint(op.f("ck_software_assignments_target"), "software_assignments", _TARGET_NEW)
    op.create_unique_constraint(
        op.f("uq_software_assignments_target"),
        "software_assignments",
        ["package_id", "building_id", "room_id", "device_id"],
        postgresql_nulls_not_distinct=True,
    )

    # ---- agent join requests: the building the installer typed, and the one it matched
    op.add_column("agent_join_requests", sa.Column("building_hint", sa.String(length=200), nullable=True))
    op.add_column("agent_join_requests", sa.Column("building_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_agent_join_requests_building_id_buildings"),
        "agent_join_requests",
        "buildings",
        ["building_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Going back is lossy by nature: what only the new model can express is dropped
    # first, so the old constraints can be restored on whatever data exists now.
    #  - building-scoped role assignments, and the roles the old code doesn't know
    #    (manager, unit_manager), are deleted (nobody gains rights by it);
    #  - software assignments and policy assignments aimed at a building are deleted;
    #  - building-owned policies / schedules become school-wide, and get the building's
    #    name appended so they can't collide with others of the same name.
    op.drop_constraint(op.f("fk_agent_join_requests_building_id_buildings"), "agent_join_requests", type_="foreignkey")
    op.drop_column("agent_join_requests", "building_id")
    op.drop_column("agent_join_requests", "building_hint")

    op.execute("DELETE FROM software_assignments WHERE target_type = 'building' OR building_id IS NOT NULL")
    op.drop_constraint(op.f("uq_software_assignments_target"), "software_assignments", type_="unique")
    op.drop_constraint(op.f("ck_software_assignments_target"), "software_assignments", type_="check")
    _drop_building_fk("software_assignments", index=True)
    op.create_check_constraint(op.f("ck_software_assignments_target"), "software_assignments", _TARGET_OLD)
    op.create_unique_constraint(
        op.f("uq_software_assignments_package_id_room_id_device_id"),
        "software_assignments",
        ["package_id", "room_id", "device_id"],
        postgresql_nulls_not_distinct=True,
    )

    op.drop_index("ix_audit_log_building_ids", table_name="audit_log")
    op.drop_column("audit_log", "building_ids")

    for table, new_uq, old_uq, cols in (
        ("schedules", "uq_schedules_org_id_building_id_name", "uq_schedules_org_id_name", ["org_id", "name"]),
        (
            "policies",
            "uq_policies_org_id_building_id_kind_name",
            "uq_policies_org_id_kind_name",
            ["org_id", "kind", "name"],
        ),
    ):
        op.execute(
            f"""
            UPDATE {table} t
            SET name = left(left(t.name, greatest(0, 200 - length(b.name) - 3)) || ' (' || b.name || ')', 200)
            FROM buildings b
            WHERE t.building_id = b.id
            """
        )
        op.drop_constraint(op.f(new_uq), table, type_="unique")
        _drop_building_fk(table, index=True)
        op.create_unique_constraint(op.f(old_uq), table, cols)
    op.execute("DELETE FROM policy_assignments WHERE target_type = 'building'")

    op.drop_table("group_buildings")

    op.execute("DELETE FROM role_assignments WHERE building_id IS NOT NULL OR role IN ('manager', 'unit_manager')")
    op.drop_constraint(op.f("uq_role_assignments_user_id_role_room_id_building_id"), "role_assignments", type_="unique")
    op.drop_constraint(op.f("ck_role_assignments_one_scope"), "role_assignments", type_="check")
    _drop_building_fk("role_assignments", index=True)
    op.create_unique_constraint(
        op.f("uq_role_assignments_user_id_role_room_id"),
        "role_assignments",
        ["user_id", "role", "room_id"],
        postgresql_nulls_not_distinct=True,
    )

    # The text column comes back, filled from the buildings' names.
    op.add_column("rooms", sa.Column("building", sa.String(length=200), nullable=False, server_default=""))
    op.execute("UPDATE rooms SET building = b.name FROM buildings b WHERE b.id = rooms.building_id")
    op.alter_column("rooms", "building", server_default=None)
    _drop_building_fk("rooms", index=True)

    op.drop_index(op.f("ix_buildings_org_id"), table_name="buildings")
    op.drop_table("buildings")
