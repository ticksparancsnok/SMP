# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Software deployment tables.

    software_uploads        an installer file an admin uploaded (stored on disk by SHA-256)
    software_packages       what to install: a file + how to run, detect and remove it
    software_assignments    package -> the whole school / a room / one computer, install or remove
    software_device_states  what each computer last reported per package
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smp.core.db import Base, OrgScoped, Timestamps, UUIDPk, utcnow
from smp.core.models import JSONType


class SoftwareUpload(UUIDPk, OrgScoped, Base):
    """One uploaded installer. The bytes live in SMP_SOFTWARE_DIR/files/<sha256> (shared
    by identical uploads); a row nobody's package uses is removed at the first clean-up a day after the upload
    (storage.collect_garbage: at server start and after each upload)."""

    __tablename__ = "software_uploads"

    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(8))  # "msi" | "exe" (from the file's content)
    file_name: Mapped[str] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SoftwarePackage(UUIDPk, Timestamps, OrgScoped, Base):
    """A program to put on computers. Replacing its file (a new version) makes every
    computer that has it install it again (the agent compares SHA-256)."""

    __tablename__ = "software_packages"

    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(64), default="")  # shown only; the file decides
    publisher: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    upload_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("software_uploads.id", ondelete="RESTRICT"))
    # MSI: extra arguments after `msiexec /i <file> /qn /norestart` (e.g. PROPERTY=value).
    # EXE: the whole silent-install command line tail (e.g. "/S" or "--silent").
    install_args: Mapped[str] = mapped_column(String(2000), default="")
    # EXE: the full uninstall command line; empty = can't be removed by SMP.
    # MSI: not needed (msiexec /x <product code>).
    uninstall_command: Mapped[str] = mapped_column(String(2000), default="")
    # How the agent tells whether it's installed: {"type": "msi" | "registry" | "file", ...}
    detection: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # "never": don't restart (a needed restart shows as reboot_pending);
    # "if_required": restart when the installer says so (3010, 1641); "always".
    reboot: Mapped[str] = mapped_column(String(16), default="never")
    # Exit codes that also mean success, besides 0 / 3010 / 1641.
    success_codes: Mapped[list[int]] = mapped_column(JSONType, default=list)
    timeout_s: Mapped[int] = mapped_column(Integer, default=3600)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    upload: Mapped[SoftwareUpload] = relationship(lazy="joined")


class SoftwareAssignment(UUIDPk, Timestamps, OrgScoped, Base):
    """Package -> target. Per package and computer, the most specific target wins
    (computer > room > school), so "install everywhere, except remove it on PC-07" works.
    Exactly one of room_id / device_id is set, except for the whole school (neither)."""

    __tablename__ = "software_assignments"
    __table_args__ = (
        # One assignment per package and target (NULLs equal: one school-wide one).
        UniqueConstraint("package_id", "room_id", "device_id", postgresql_nulls_not_distinct=True),
        CheckConstraint(
            "(target_type = 'org' AND room_id IS NULL AND device_id IS NULL)"
            " OR (target_type = 'room' AND room_id IS NOT NULL AND device_id IS NULL)"
            " OR (target_type = 'device' AND device_id IS NOT NULL AND room_id IS NULL)",
            name="target",
        ),
    )

    package_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("software_packages.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(String(8))  # org | room | device
    room_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(8))  # install | remove
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class SoftwareDeviceState(OrgScoped, Base):
    """The last thing a computer's agent reported about a package. `sha256` says which
    file it was about, so a state from before the package got a new file isn't
    mistaken for the current one."""

    __tablename__ = "software_device_states"

    device_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    package_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("software_packages.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    state: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str | None] = mapped_column(String(64))
    exit_code: Mapped[int | None] = mapped_column(BigInteger)  # Windows exit codes are 32-bit unsigned
    message: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
