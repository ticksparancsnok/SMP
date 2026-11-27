# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""The agent, handed out by SMP itself: to PCs being installed, and as automatic updates.

The server's Docker image compiles smp-agent.exe from agent/ (see server/Dockerfile),
so every SMP install offers the agent that matches its own version ([agent] in
Version.toml, written next to it as smp-agent.version), with nothing downloaded from
anywhere else:

    https://<smp>/downloads/smp-agent.exe        no sign-in: fetch it right on the PC
    GET /api/v1/agent/download                   version, size, SHA-256 (Devices page)

Installed agents get the same file as an update when its version differs from theirs
(api.py: the "update" entry of the heartbeat config, with the SHA-256 they check).

Builds are per platform (the agent reports its own). The image builds only Windows; a
Linux build (smp-agent-linux-x86_64 + .version in the same folder) is picked up if
someone puts it there, e.g. for development and tests.

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


@dataclass(frozen=True)
class Platform:
    name: str  # what the agent reports, e.g. "windows-x86_64"
    file: str  # in SMP_AGENT_DOWNLOAD_DIR, served at /downloads/<file>
    version_file: str
    media_type: str


PLATFORMS = {
    p.name: p
    for p in (
        Platform("windows-x86_64", "smp-agent.exe", "smp-agent.version", "application/vnd.microsoft.portable-executable"),
        Platform("linux-x86_64", "smp-agent-linux-x86_64", "smp-agent-linux-x86_64.version", "application/octet-stream"),
    )
}
DEFAULT_PLATFORM = "windows-x86_64"  # agents that don't say which they are
EXE_NAME = PLATFORMS[DEFAULT_PLATFORM].file
URL_PATH = f"/downloads/{EXE_NAME}"


def url_path(platform: Platform) -> str:
    return f"/downloads/{platform.file}"


def platform_for_file(file_name: str) -> Platform | None:
    return next((p for p in PLATFORMS.values() if p.file == file_name), None)


@dataclass(frozen=True)
class AgentBuild:
    platform: Platform
    path: Path
    size: int
    sha256: str
    version: str | None

    @property
    def url(self) -> str:
        return url_path(self.platform)


def current_build(platform: str | None = None) -> AgentBuild | None:
    """The agent this server offers for `platform` (default: Windows), or None (none
    built, e.g. a development server, or an unknown platform). Hashing 4 MB takes a few
    milliseconds and happens once per file version."""
    folder = get_settings().agent_download_dir
    p = PLATFORMS.get(platform or DEFAULT_PLATFORM)
    if not folder or p is None:
        return None
    path = Path(folder) / p.file
    try:
        stat = path.stat()
    except OSError:
        return None
    return _describe(p, path, stat.st_size, _identity(path), _identity(path.parent / p.version_file))


def _identity(path: Path) -> tuple[int, ...]:
    """What tells a changed file from an unchanged one, for the cache. The change time
    (ctime) is in it because copies keep the modification time, sometimes only to the
    second: two builds of the same size copied in the same second looked alike."""
    try:
        st = path.stat()
    except OSError:
        return ()
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@lru_cache(maxsize=8)
def _describe(platform: Platform, path: Path, size: int, exe_id: tuple, version_id: tuple) -> AgentBuild:  # *_id: cache key
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        version = (path.parent / platform.version_file).read_text().strip() or None
    except OSError:
        version = None
    return AgentBuild(platform=platform, path=path, size=size, sha256=sha256, version=version)
