# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""The shared domain model.

Every module works in terms of these entities. The point of one product instead of
several separate tools is that DNS, classroom, software deployment and identity all
speak about the same Device / Room / User / Group / Policy / Schedule.

    Organization (a school / "unit"; every table is scoped to one)
     ├── User ──< GroupMember >── Group        (people, classes, staff lists)
     │                             └──< GroupBuilding >── Building  (none = school-wide)
     ├── RoleAssignment (user, role, optional scope: one room OR one building)
     ├── Building ──< Room (has subnets) ──< Device   (how teachers think about machines)
     ├── Policy (kind = module name, data validated by that module's schema;
     │           optional owner building, NULL = school-wide)
     │    └──< PolicyAssignment ──> target (org | building | room | group | device | user)
     │                          └──> Schedule (optional time windows)
     ├── ModuleSetting (per-org settings for each module)
     ├── ApiToken / AuthSession                (machine + browser credentials)
     └── AuditLog                              (append-only; building_ids = who may see it)

The roles and where each may be assigned are a table in smp/core/roles.py.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smp.core.db import Base, OrgScoped, Timestamps, UUIDPk, utcnow

# JSONB on Postgres, plain JSON elsewhere.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Organization(UUIDPk, Timestamps, Base):
    __tablename__ = "organizations"

    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Budapest")


# --------------------------------------------------------------------------- people


class AuthSource(enum.StrEnum):
    """Where a user's credentials live. 'local' = password hash in our DB.
    The identity module will add ldap / google / entra providers."""

    LOCAL = "local"


class User(UUIDPk, Timestamps, OrgScoped, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("org_id", "username"),
        # One account per e-mail address within a school (case-insensitive); e-mail is
        # optional. Directory sync will match accounts by it.
        Index(
            "uq_users_org_email_ci",
            "org_id",
            func.lower(text("email")),
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
    )

    username: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str | None] = mapped_column(String(320))
    auth_source: Mapped[str] = mapped_column(String(32), default=AuthSource.LOCAL)
    # ID in the upstream directory (Google/Entra/LDAP) for sync; None for local users.
    external_id: Mapped[str | None] = mapped_column(String(256))
    password_hash: Mapped[str | None] = mapped_column(String(256))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set whenever someone else chose the password (admin-set password or a one-time
    # password). Until cleared by changing it, the API only allows /auth/me,
    # /auth/password, /auth/logout and /modules.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Active one-time password: Fernet-encrypted copy (so admins can export login
    # slips until it's used) and expiry. password_hash holds its argon2 hash; both
    # fields are cleared when the user sets their own password.
    otp_secret: Mapped[str | None] = mapped_column(Text)
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The user's own UI settings (e.g. rows per page per list), so they follow the
    # account to any browser. Shape: PreferencesOut in smp/core/api/auth.py.
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, server_default=text("'{}'"))

    roles: Mapped[list["RoleAssignment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    # Read-only view of memberships; change them through GroupMember rows.
    groups: Mapped[list["Group"]] = relationship(
        secondary="group_members", viewonly=True, lazy="selectin", order_by="Group.name"
    )


class Group(UUIDPk, Timestamps, OrgScoped, Base):
    """A set of users: a class ("10.B"), a staff list, a club..."""

    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32), default="custom")  # class | staff | custom
    description: Mapped[str] = mapped_column(Text, default="")
    external_id: Mapped[str | None] = mapped_column(String(256))

    teachers: Mapped[list["User"]] = relationship(
        secondary="group_teachers", viewonly=True, lazy="selectin", order_by="User.display_name"
    )
    # The buildings the group belongs to (several are fine); none = a school-wide group.
    # Read-only view: change them through GroupBuilding rows.
    buildings: Mapped[list["Building"]] = relationship(
        secondary="group_buildings", viewonly=True, lazy="selectin", order_by="Building.name"
    )


class GroupMember(OrgScoped, Base):
    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class GroupTeacher(OrgScoped, Base):
    """Teachers responsible for a group (e.g. the form teacher of 10.B). Kept apart from
    GroupMember on purpose: membership drives policies and class lists, and a teacher
    must not inherit the students' policies by being "in" the class."""

    __tablename__ = "group_teachers"

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class GroupBuilding(OrgScoped, Base):
    """A group belongs to a building (a class that sits in it). A group may belong to
    several buildings; one that belongs to none is school-wide."""

    __tablename__ = "group_buildings"

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    building_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("buildings.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Role(enum.StrEnum):
    """The role names. What each one is allowed (scopes, inheritance, who may hand it
    out) is the table in smp/core/roles.py; what it may do is the permissions' default
    roles in the module manifests."""

    ADMIN = "admin"
    UNIT_MANAGER = "unit_manager"
    MANAGER = "manager"
    TEACHER = "teacher"
    STUDENT = "student"


class RoleAssignment(UUIDPk, OrgScoped, Base):
    """Grants a role to a user: org-wide (room_id and building_id both NULL), for one
    building (building_id) or for one room (room_id), never both.

    Example: a teacher assigned to rooms 'Lab 1' and 'Lab 2' gets teacher permissions
    only for devices in those rooms (e.g. may lock screens there, nowhere else); a
    manager assigned to building 'Main' administers that building and nothing else.
    Which role may sit at which scope: smp/core/roles.py.
    """

    __tablename__ = "role_assignments"
    __table_args__ = (
        UniqueConstraint("user_id", "role", "room_id", "building_id", postgresql_nulls_not_distinct=True),
        CheckConstraint("room_id IS NULL OR building_id IS NULL", name="one_scope"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    room_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("rooms.id", ondelete="CASCADE"))
    building_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("buildings.id", ondelete="CASCADE"), index=True
    )

    user: Mapped[User] = relationship(back_populates="roles")


# --------------------------------------------------------------------------- places & machines


class Building(UUIDPk, Timestamps, OrgScoped, Base):
    """A building of the school: the scope a manager administers. Rooms sit in one
    (or in none)."""

    __tablename__ = "buildings"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")


class Room(UUIDPk, Timestamps, OrgScoped, Base):
    """A physical location. Subnets let the DNS module map a client IP to a room even
    for machines that have no agent (phones, BYOD, unmanaged PCs)."""

    __tablename__ = "rooms"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    name: Mapped[str] = mapped_column(String(200))
    # None: not in any building (only org-wide roles reach such a room).
    building_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("buildings.id", ondelete="SET NULL"), index=True
    )
    description: Mapped[str] = mapped_column(Text, default="")
    # List of CIDR strings, e.g. ["10.0.12.0/24"].
    subnets: Mapped[list[str]] = mapped_column(JSONType, default=list)

    building: Mapped[Building | None] = relationship(lazy="joined")


class DeviceStatus(enum.StrEnum):
    PENDING = "pending"  # pre-registered or enrolment requested
    ACTIVE = "active"  # enrolled, has a certificate
    RETIRED = "retired"  # certificate revoked; kept for history


class Device(UUIDPk, Timestamps, OrgScoped, Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("org_id", "agent_id"),
        Index("ix_devices_org_last_ip", "org_id", "last_ip"),
    )

    hostname: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255), default="")
    mac: Mapped[str | None] = mapped_column(String(17))  # normalised aa:bb:cc:dd:ee:ff
    # The device's own address: reported by its agent (the address its system uses to
    # reach SMP), else the one SMP sees. observed_ip: the address SMP sees the agent's
    # requests come from; differs from last_ip behind a NAT (e.g. a lab with its own router).
    last_ip: Mapped[str | None] = mapped_column(String(45))
    observed_ip: Mapped[str | None] = mapped_column(String(45))
    room_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("rooms.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=DeviceStatus.PENDING)

    # Filled in by the agent module. agent_id is the SHA-256 fingerprint (hex) of the
    # agent's public key: the device's identity, independent of its IP address.
    agent_id: Mapped[str | None] = mapped_column(String(64))
    agent_public_key: Mapped[str | None] = mapped_column(Text)  # P-256 point, base64url
    # "fixed": the room is set by an admin (desktops). "network": the room follows the
    # subnet of the IP the agent reports from (laptops moving between VLANs / APs).
    room_mode: Mapped[str] = mapped_column(String(16), default="fixed", server_default="fixed")
    logged_in_user: Mapped[str | None] = mapped_column(String(256))  # console session user
    agent_version: Mapped[str | None] = mapped_column(String(32))
    # What the PC's agent can do, as it reports in each heartbeat (e.g. ["classroom",
    # "lock", "remote", ...]): the UI offers a feature only to agents that have it.
    # None: an agent from before 1.0.4 (it doesn't report them).
    agent_capabilities: Mapped[list | None] = mapped_column(JSONType)
    cert_fingerprint: Mapped[str | None] = mapped_column(String(128))
    os_name: Mapped[str | None] = mapped_column(String(64))
    os_version: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    notes: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------------------------- policy


class Schedule(UUIDPk, Timestamps, OrgScoped, Base):
    """Recurring weekly windows, e.g. lessons: [{"days":[0,1,2,3,4],"start":"08:00","end":"08:45"}].
    days: 0 = Monday ... 6 = Sunday. Times are local to `timezone`."""

    __tablename__ = "schedules"
    __table_args__ = (UniqueConstraint("org_id", "building_id", "name", postgresql_nulls_not_distinct=True),)

    # The building that owns it, who may edit it; None = school-wide (admins edit).
    building_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("buildings.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Budapest")
    windows: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)


class Policy(UUIDPk, Timestamps, OrgScoped, Base):
    """A named bundle of settings for one module ("kind" = module name).
    The module validates `data` with its policy_model, e.g. a 'dns' policy holds
    blocked categories, a 'classroom' policy the monitoring frame rate. (Software has
    its own admin-only assignments instead: see smp/modules/software/__init__.py.)"""

    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("org_id", "building_id", "kind", "name", postgresql_nulls_not_distinct=True),)

    # The building that owns it, who may edit it; None = school-wide (admins edit).
    building_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("buildings.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class TargetType(enum.StrEnum):
    ORG = "org"
    BUILDING = "building"
    ROOM = "room"
    GROUP = "group"
    DEVICE = "device"
    USER = "user"


class PolicyAssignment(UUIDPk, Timestamps, OrgScoped, Base):
    """Applies a policy to a target, optionally only during a schedule and/or a fixed
    time range. "Exam mode" for a room = several assignments (dns, classroom, ...)
    with the same label, a high priority and an ends_at."""

    __tablename__ = "policy_assignments"
    __table_args__ = (Index("ix_policy_assignments_target", "org_id", "target_type", "target_id"),)

    policy_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("policies.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # NULL when target_type == org
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("schedules.id", ondelete="SET NULL"))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    label: Mapped[str] = mapped_column(String(100), default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    policy: Mapped[Policy] = relationship(lazy="joined")


# --------------------------------------------------------------------------- platform


class ModuleSetting(Base):
    __tablename__ = "module_settings"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    module: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class AuthSession(UUIDPk, OrgScoped, Base):
    """Browser login session. Only the SHA-256 of the token is stored."""

    __tablename__ = "auth_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(400))


class ApiToken(UUIDPk, OrgScoped, Base):
    """Credential for services (the DNS container, the agent gateway, scripts).
    Carries an explicit permission list instead of a role."""

    __tablename__ = "api_tokens"

    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(16))  # shown in UI to identify the token
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    permissions: Mapped[list[str]] = mapped_column(JSONType, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only. The initial migration installs a trigger that rejects UPDATE and
    DELETE (except an explicit retention purge, see migrations)."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_org_at", "org_id", "at"),
        Index("ix_audit_log_org_action", "org_id", "action"),
        Index("ix_audit_log_org_target", "org_id", "target_type", "target_id"),
        Index("ix_audit_log_building_ids", "building_ids", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), default=utcnow)
    actor_type: Mapped[str] = mapped_column(String(16))  # user | token | system | device
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_name: Mapped[str] = mapped_column(String(200), default="")
    action: Mapped[str] = mapped_column(String(128))  # "<module>.<entity>.<verb>"
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # The buildings the entry is about (managers of those buildings may read it). NULL:
    # an org-level entry, for admins and unit managers only. No foreign key: history
    # outlives a deleted building.
    building_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(400))
