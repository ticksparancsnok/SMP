# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, Uuid
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
