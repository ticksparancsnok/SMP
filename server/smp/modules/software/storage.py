# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Installer files on the server's disk, and handing them out without flooding the network.

Layout under SMP_SOFTWARE_DIR (a Docker volume in the stack):

    files/<sha256>        every uploaded installer, once per content
    incoming/<id>.part    uploads still arriving

Uploads are streamed to disk (installers are often hundreds of MB; nothing is held in
memory), hashed on the way, and checked to really be an .msi / .exe by their first bytes.

Downloads: only signed agents whose computer should have the package get the file
(api.py; installers can contain license keys, so they are not public like the agent).
Schools often have 100 Mbps: DownloadSlots caps how many computers download at once
(Settings: max_parallel_downloads); the others are told to come back later.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from fastapi import Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from smp.core.config import get_settings
from smp.core.errors import AppError

log = logging.getLogger(__name__)

# Windows Installer packages are OLE compound files; programs start with "MZ".
MSI_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
EXE_MAGIC = b"MZ"
KINDS = {".msi": ("msi", MSI_MAGIC), ".exe": ("exe", EXE_MAGIC)}
# Keep this much disk free after an upload: a full disk would stop the database too.
FREE_MARGIN = 1024**3
# An upload no package uses is kept this long (time to fill in the package form).
ORPHAN_TTL = timedelta(days=1)
# Files and partial uploads younger than this are never removed (an upload may be
# between "file stored" and "row committed").
FILE_GRACE_S = 3600
WRITE_CHUNK = 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def root() -> Path:
    """SMP_SOFTWARE_DIR, or 503 when this server has nowhere to keep installers."""
    folder = get_settings().software_dir
    if not folder:
        raise AppError(status.HTTP_503_SERVICE_UNAVAILABLE, "software.no_storage",
                       "this SMP server has no folder for installers (SMP_SOFTWARE_DIR)")
    return Path(folder)


def file_path(sha256: str) -> Path:
    if not SHA256_RE.match(sha256):  # it becomes a path: never anything else than hex
        raise ValueError("not a SHA-256")
    return root() / "files" / sha256


def clean_file_name(name: str) -> str:
    """The base name only (an upload's name is just a label), without control characters."""
    base = PurePosixPath(name.replace("\\", "/")).name
    return "".join(c for c in base if c.isprintable())[:255].strip()


@dataclass(frozen=True)
class Received:
    sha256: str
    size: int
    kind: str  # "msi" | "exe"
    file_name: str


def _bad_file() -> AppError:
    return AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "software.bad_file",
                    "only Windows Installer packages (.msi) and programs (.exe) can be uploaded")


def _too_large(limit: int) -> AppError:
    return AppError(status.HTTP_413_CONTENT_TOO_LARGE, "software.too_large",
                    f"the file is larger than {limit // 1024**2} MB", max_mb=limit // 1024**2)


def _write(f: BinaryIO, hasher, data: bytes) -> None:
    # In a worker thread: hashing and disk writes shouldn't hold up the event loop.
    hasher.update(data)
    f.write(data)


def _finish(f: BinaryIO) -> None:
    f.flush()
    os.fsync(f.fileno())
    f.close()


async def receive(request: Request, file_name: str) -> Received:
    """Store the request body as an installer. Raises AppError for anything that isn't
    one (checked by extension AND content), too large, or no room on the disk."""
    base = root()
    name = clean_file_name(file_name)
    kind, magic = KINDS.get(Path(name).suffix.lower(), (None, b""))
    if not name or kind is None:
        raise _bad_file()
    limit = get_settings().software_max_upload_bytes
    declared = int(request.headers.get("content-length") or 0)
    if declared > limit:
        raise _too_large(limit)
    for sub in ("files", "incoming"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(base).free < declared + FREE_MARGIN:
        raise AppError(status.HTTP_507_INSUFFICIENT_STORAGE, "software.disk_full",
                       "not enough free disk space on the SMP server for this file")

    part = base / "incoming" / f"{uuid.uuid4().hex}.part"
    hasher = hashlib.sha256()
    size, head, buf = 0, b"", bytearray()
    f = await run_in_threadpool(open, part, "wb")
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise _too_large(limit)
            if len(head) < len(MSI_MAGIC):
                head += chunk[: len(MSI_MAGIC) - len(head)]
                if len(head) >= len(magic) and not head.startswith(magic):
                    raise _bad_file()  # say so at once, not after 2 GB
            buf += chunk
            if len(buf) >= WRITE_CHUNK:
                await run_in_threadpool(_write, f, hasher, bytes(buf))
                buf.clear()
        if buf:
            await run_in_threadpool(_write, f, hasher, bytes(buf))
        await run_in_threadpool(_finish, f)
        if size == 0 or not head.startswith(magic):
            raise _bad_file()
    except BaseException:  # also a client that went away (ClientDisconnect), or cancellation
        f.close()
        part.unlink(missing_ok=True)
        raise

    digest = hasher.hexdigest()
    target = base / "files" / digest
    if target.exists():
        part.unlink(missing_ok=True)
        os.utime(target)  # "recent": the clean-up below leaves it alone until our row exists
    else:
        os.replace(part, target)
    return Received(sha256=digest, size=size, kind=kind, file_name=name)


async def collect_garbage(db: AsyncSession) -> int:
    """Remove uploads no package uses (after ORPHAN_TTL), files no upload row refers to,
    and abandoned partial uploads. Returns how many files were deleted. Safe to run any
    time; a failure only means leftovers stay until the next run."""
    from smp.modules.software.models import SoftwarePackage, SoftwareUpload

    cutoff = datetime.now(UTC) - ORPHAN_TTL
    used = exists().where(SoftwarePackage.upload_id == SoftwareUpload.id)
    await db.execute(delete(SoftwareUpload).where(SoftwareUpload.created_at < cutoff, ~used))
    await db.commit()
    folder = get_settings().software_dir
    if not folder:
        return 0
    keep = set((await db.execute(select(SoftwareUpload.sha256).distinct())).scalars())
    return await run_in_threadpool(_remove_unused_files, Path(folder), keep)


def _remove_unused_files(base: Path, keep: set[str]) -> int:
    now, removed = time.time(), 0
    for sub, wanted in (("files", lambda p: p.name in keep), ("incoming", lambda p: False)):
        folder = base / sub
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            try:
                if wanted(p) or not p.is_file() or now - p.stat().st_mtime < FILE_GRACE_S:
                    continue
                p.unlink()
                removed += 1
            except OSError as e:
                log.warning("software: couldn't remove %s: %s", p, e)
    if removed:
        log.info("software: removed %d unused installer file(s)", removed)
    return removed


# --------------------------------------------------------------------------- downloads


class DownloadSlots:
    """How many computers download installers right now (one server process).

    Everything here runs on the event loop thread, so no lock is needed. A lease that
    was never released (a response that never started streaming) expires after
    `stale_after` seconds, so a slot can't stay taken forever."""

    def __init__(self, stale_after: float = 6 * 3600, clock=time.monotonic) -> None:
        self._leases: dict[int, float] = {}
        self._next = 0
        self._stale_after = stale_after
        self._clock = clock

    def try_acquire(self, limit: int) -> Lease | None:
        now = self._clock()
        self._leases = {k: t for k, t in self._leases.items() if now - t < self._stale_after}
        if len(self._leases) >= limit:
            return None
        self._next += 1
        self._leases[self._next] = now
        return Lease(self, self._next)

    @property
    def active(self) -> int:
        return len(self._leases)

    def _release(self, lease_id: int) -> None:
        self._leases.pop(lease_id, None)


class Lease:
    def __init__(self, slots: DownloadSlots, lease_id: int) -> None:
        self._slots, self._id = slots, lease_id

    def release(self) -> None:  # idempotent
        self._slots._release(self._id)


class RangeNotSatisfiable(Exception):
    pass


def parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    """A single `Range: bytes=...` as (first, last) byte, or None for the whole file
    (no header, or one we don't support: several ranges). Lets an interrupted download
    continue where it stopped instead of starting a 500 MB file over."""
    if not value:
        return None
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", value)
    if not m or m.groups() == ("", ""):
        return None
    first, last = m.groups()
    if first == "":  # the last N bytes
        n = int(last)
        if n == 0:
            raise RangeNotSatisfiable
        return max(0, size - n), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise RangeNotSatisfiable
    return start, end
