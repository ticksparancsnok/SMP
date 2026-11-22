# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""The Windows agent, handed out by SMP itself.

The server's Docker image compiles smp-agent.exe from agent/ (see server/Dockerfile),
so every SMP install offers the agent that matches its own version, with nothing
downloaded from anywhere else:

    https://<smp>/downloads/smp-agent.exe        no sign-in: fetch it right on the PC
    GET /api/v1/agent/download                   version, size, SHA-256 (Devices page)

The exe holds no secrets (a PC does nothing until an admin approves it), so the
download is public. Its SHA-256 is computed here from the file itself, and shown in
SMP so admins can check what they copied to a PC.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from smp.core.config import get_settings

EXE_NAME = "smp-agent.exe"
URL_PATH = f"/downloads/{EXE_NAME}"


@dataclass(frozen=True)
class AgentBuild:
    path: Path
    size: int
    sha256: str
    version: str | None


def current_build() -> AgentBuild | None:
    """The agent this server offers, or None (e.g. a development server without it).
    Hashing 4 MB takes a few milliseconds and happens once per file version."""
    folder = get_settings().agent_download_dir
    if not folder:
        return None
    path = Path(folder) / EXE_NAME
    try:
        stat = path.stat()
    except OSError:
        return None
    return _describe(path, stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=4)
def _describe(path: Path, size: int, mtime_ns: int) -> AgentBuild:  # size/mtime: the cache key
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        version = (path.parent / "smp-agent.version").read_text().strip() or None
    except OSError:
        version = None
    return AgentBuild(path=path, size=size, sha256=sha256, version=version)
