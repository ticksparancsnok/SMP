# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)  # ruff: ignore[missing-copyright-notice]
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""SMP's versions, read from Version.toml in the repository root.

The only place they are written down (see the comment at the top of that file).

Where the file is found:
    1. SMP_VERSION_FILE, if set (the Docker image sets /app/Version.toml);
    2. otherwise the nearest Version.toml above this file: the repository root in a development checkout (`pip install -e .`), /app during the image's `pip install .`.

Standard library only:
    Setuptools also loads this file on its own, without the rest of the package, to fill in the package version (pyproject.toml, `dynamic = ["version"]`).

"""  # ruff: ignore[line-too-long]

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

FILE_NAME = "Version.toml"
SECTIONS = ("server", "WEBUI", "agent", "Site-blocker", "release")
# "1.0.2" or "1.0.2 C": MAJOR.MINOR.PATCH, optionally one space and a revision letter.  # ruff: ignore[line-too-long]
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?: ([A-Z]))?$")
MAX_LENGTH = 32  # agents report theirs in a 32-character column (devices.agent_version)  # ruff: ignore[line-too-long]


def version_file() -> Path:  # ruff: ignore[undocumented-public-function]
    if env := os.environ.get("SMP_VERSION_FILE"):
        return Path(env)

    for directory in Path(__file__).resolve().parents:
        if (directory / FILE_NAME).is_file():
            return directory / FILE_NAME

    msg = f"{FILE_NAME} not found above {Path(__file__).resolve().parent}; set SMP_VERSION_FILE to its path (it lives in the root of the SMP repository)"  # ruff: ignore[line-too-long]
    raise RuntimeError(msg)


def load(path: Path | None = None) -> dict[str, str]:
    """{section: version} for every section in SECTIONS. Raises ValueError, naming the file and section, when one is missing or not in the "1.0.2" / "1.0.2 C" format."""  # ruff: ignore[docstring-missing-exception, docstring-missing-returns, line-too-long]
    path = path or version_file()
    with path.open("rb") as f:
        data = tomllib.load(f)

    versions: dict[str, str] = {}

    for section in SECTIONS:
        value = data.get(section, {}).get("version", None)
        if not isinstance(value, str):
            msg = f"{path}: [{section}] has no version"
            raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]

        if not VERSION_RE.match(value) or len(value) > MAX_LENGTH:
            msg = f'{path}: [{section}] version "{value}" is not like "1.0.2" or "1.0.2 C"'  # ruff: ignore[line-too-long]
            raise ValueError(msg)

        versions[section] = value

    return versions


def pep440(version: str) -> str:
    """Python packaging can't hold "1.0.2 C"; the letter becomes a local label: 1.0.2+c."""  # ruff: ignore[docstring-missing-exception, docstring-missing-returns, line-too-long]
    m = VERSION_RE.match(version)
    if not m:
        msg = f'version "{version}" is not like "1.0.2" or "1.0.2 C"'
        raise ValueError(msg)

    major, minor, patch, letter = m.groups()

    return f"{major}.{minor}.{patch}{f'+{letter.lower()}' if letter else ''}"


VERSIONS = load()
SERVER_VERSION = VERSIONS["server"]
RELEASE_VERSION = VERSIONS["release"]
# Read by setuptools (pyproject.toml) for the installed package's metadata.
PACKAGE_VERSION = pep440(SERVER_VERSION)
