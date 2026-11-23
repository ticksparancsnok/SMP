# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Version.toml is the only place SMP's versions are written (no database needed here).

Checks its format, that the line-based reader the Docker builds use (tools/version.sh)
agrees with a real TOML parser, and that no component got a version of its own back."""

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

import smp
from smp import version
from smp.core.core_module import CoreModule

ROOT = Path(__file__).resolve().parents[2]
VERSION_TOML = ROOT / "Version.toml"


def test_version_toml_is_valid_and_complete():
    versions = version.load(VERSION_TOML)
    assert set(versions) == set(version.SECTIONS)
    for section, value in versions.items():
        assert version.VERSION_RE.match(value), (section, value)


def test_server_reports_the_server_section():
    server = tomllib.loads(VERSION_TOML.read_text())["server"]["version"]
    assert smp.__version__ == version.SERVER_VERSION == server
    assert CoreModule.version == server  # the sidebar shows this one


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("awk") is None, reason="needs sh + awk")
@pytest.mark.parametrize("section", version.SECTIONS)
def test_shell_reader_agrees_with_toml(section):
    expected = tomllib.loads(VERSION_TOML.read_text())[section]["version"]
    out = subprocess.run(["sh", str(ROOT / "tools" / "version.sh"), section, str(VERSION_TOML)],
                         capture_output=True, text=True, check=True)
    assert out.stdout == expected + "\n"


def test_shell_reader_fails_for_unknown_section():
    if shutil.which("sh") is None:
        pytest.skip("needs sh")
    out = subprocess.run(["sh", str(ROOT / "tools" / "version.sh"), "nope", str(VERSION_TOML)],
                         capture_output=True, text=True)
    assert out.returncode != 0 and out.stdout == "" and "[nope]" in out.stderr


@pytest.mark.parametrize(
    ("value", "pep440"),
    [("1.0.2", "1.0.2"), ("1.0.2 C", "1.0.2+c"), ("10.20.30 A", "10.20.30+a")],
)
def test_pep440(value, pep440):
    assert version.pep440(value) == pep440


@pytest.mark.parametrize("bad", ["1.0", "1.0.2C", "1.0.2 c", "1.0.2  C", "v1.0.2", "1.0.2 AB", ""])
def test_bad_versions_are_refused(tmp_path, bad):
    text = VERSION_TOML.read_text().replace('version = "1.0.0 A"', f'version = "{bad}"')
    assert f'"{bad}"' in text  # the Site-blocker line was replaced
    f = tmp_path / "Version.toml"
    f.write_text(text)
    with pytest.raises(ValueError, match=r"\[Site-blocker\]"):
        version.load(f)


def test_missing_section_is_refused(tmp_path):
    f = tmp_path / "Version.toml"
    f.write_text('[server]\nversion = "1.0.0"\n')
    with pytest.raises(ValueError, match=r"\[WEBUI\] has no version"):
        version.load(f)


def test_smp_version_file_overrides_the_search(tmp_path, monkeypatch):
    f = tmp_path / "elsewhere.toml"
    monkeypatch.setenv("SMP_VERSION_FILE", str(f))
    assert version.version_file() == f


def test_no_component_keeps_its_own_version():
    """Each of these used to hold a copy that drifted from the others."""
    pyproject = tomllib.loads((ROOT / "server" / "pyproject.toml").read_text())
    assert "version" not in pyproject["project"] and "version" in pyproject["project"]["dynamic"]
    assert "version" not in json.loads((ROOT / "WEBUI" / "package.json").read_text())
    assert "version" not in tomllib.loads((ROOT / "agent" / "Cargo.toml").read_text())["workspace"]["package"]
    assert "version" not in tomllib.loads((ROOT / "agent" / "smp-agent" / "Cargo.toml").read_text())["package"]
    assert "__version__ = " not in (ROOT / "server" / "smp" / "__init__.py").read_text()
