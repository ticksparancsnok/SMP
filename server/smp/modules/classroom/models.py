# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

import uuid

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smp.core.db import Base, OrgScoped, Timestamps, UUIDPk
from smp.core.models import JSONType


class ClassroomLayout(UUIDPk, Timestamps, OrgScoped, Base):
    """How a room's screens are arranged on the monitoring page.

    mode "auto": sorted by hostname. mode "grid": `rows` x `cols` cells, and
    `positions` maps device id -> [row, col] (0-based) as the PCs stand in the room.
    PCs without a position are listed below the grid."""

    __tablename__ = "classroom_layouts"
    __table_args__ = (UniqueConstraint("org_id", "room_id"),)

    room_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("rooms.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(8), default="auto")
    rows: Mapped[int] = mapped_column(Integer, default=4)
    cols: Mapped[int] = mapped_column(Integer, default=6)
    positions: Mapped[dict] = mapped_column(JSONType, default=dict)


class ClassroomLock(UUIDPk, Timestamps, OrgScoped, Base):
    """A locked computer: its screen shows `message` with the school's logo, and its
    keyboard and mouse are blocked. Only while it is in the room it was locked in
    (`room_id`): a laptop that leaves the room is free again, and locked again if it
    comes back before anyone unlocked it. One lock per computer."""

    __tablename__ = "classroom_locks"
    __table_args__ = (UniqueConstraint("device_id"),)

    room_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("devices.id", ondelete="CASCADE"))
    message: Mapped[str] = mapped_column(Text, default="")  # empty: the agent's built-in sentence
    locked_by: Mapped[str] = mapped_column(String(255), default="")  # who locked it (shown in SMP)


class ClassroomLockLogo(UUIDPk, Timestamps, OrgScoped, Base):
    """The school's logo on locked screens (one per school; none: the SMP logo). Always
    PNG: the web UI converts other image types before uploading. Agents download it
    once and keep it by its SHA-256."""

    __tablename__ = "classroom_lock_logos"
    __table_args__ = (UniqueConstraint("org_id"),)

    png: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
