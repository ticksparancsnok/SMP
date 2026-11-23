# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""install.sh / upgrade.sh plumbing, without Docker: tools/release.sh (putting a release
into an install folder), install.sh --fetch (download + unpack) and version comparison.

The whole install/upgrade (containers, database) isn't run here; see the PR notes."""

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELEASE_SH = ROOT / "tools" / "release.sh"
VERSION_SH = ROOT / "tools" / "version.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("sha256sum") is None, reason="needs bash + coreutils"
)

# The files smp_check_release requires, plus a few to play with.
BASE = {
    "Version.toml": '[release]\nversion = "1.0.0"\n',
    "docker-compose.yml": "services: {}\n",
    "install.sh": "#!/usr/bin/env bash\necho install\n",
    "upgrade.sh": "#!/usr/bin/env bash\necho upgrade\n",
    "tools/version.sh": VERSION_SH.read_text(),
    "tools/release.sh": RELEASE_SH.read_text(),
    "tools/db-backup.sh": (ROOT / "tools" / "db-backup.sh").read_text(),
    "server/Dockerfile": "FROM scratch\n",
    ".env.example": "SMP_SECRET_KEY=\n",
    "server/smp/migrations/versions/0001.py": "one\n",
    "server/smp/migrations/versions/0002_dropped.py": "two\n",
    "docs/a.md": "a\n",
}


def make_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel.endswith(".sh"):
            p.chmod(0o755)
    return root


def release(tmp_path: Path, name: str, **changes: str | None) -> Path:
    files = dict(BASE)
    for rel, text in changes.items():
        rel = rel.replace("__", "/")
        if text is None:
            files.pop(rel, None)
        else:
            files[rel] = text
    return make_tree(tmp_path / name, files)


def install(new: Path, target: Path, plan: Path, backup: Path | None = None) -> dict[str, list[str]]:
    script = f'set -euo pipefail; source "{RELEASE_SH}"; smp_plan "$1" "$2" "$3"; smp_apply "$1" "$2" "$3" {"\"$4\"" if backup else ""}'
    subprocess.run(["bash", "-c", script, "-", str(new), str(target), str(plan), str(backup or "")],
                   check=True, capture_output=True, text=True)
    return {n: (plan / n).read_text().split() for n in ("added", "changed", "removed", "modified")}


def test_first_install_copies_everything_and_writes_a_manifest(tmp_path):
    new = release(tmp_path, "r1")
    target = tmp_path / "smp"
    target.mkdir()
    (target / "install.sh").write_text("the downloaded installer\n")  # replaced
    lists = install(new, target, tmp_path / "plan")
    assert sorted(lists["added"]) == sorted(set(BASE) - {"install.sh"})
    assert lists["changed"] == ["install.sh"] and lists["removed"] == [] and lists["modified"] == []
    for rel, text in BASE.items():
        assert (target / rel).read_text() == text
    assert os.access(target / "tools" / "version.sh", os.X_OK)  # modes are kept
    manifest = (target / ".smp-manifest").read_text().splitlines()
    assert len(manifest) == len(BASE)
    assert f"{hashlib.sha256(b'a\n').hexdigest()}  docs/a.md" in manifest


def test_upgrade_replaces_removes_and_backs_up_but_never_touches_local_state(tmp_path):
    target = tmp_path / "smp"
    target.mkdir()
    install(release(tmp_path, "r1"), target, tmp_path / "plan1")

    # Local state that must survive, whatever the release contains.
    (target / ".env").write_text("SMP_SECRET_KEY=keep-me\n")
    make_tree(target, {"nginx/certs/fullchain.pem": "pinned cert\n", "nginx/certs/privkey.pem": "key\n",
                       "backups/old/x": "x\n", "my-notes.txt": "mine\n"})
    (target / "docs" / "a.md").write_text("edited by hand\n")  # changed locally, and in the release
    old_upgrade = target / "upgrade.sh"
    os.link(old_upgrade, tmp_path / "running-upgrade.sh")  # what a running bash still reads

    new = release(
        tmp_path, "r2",
        **{"Version.toml": '[release]\nversion = "1.0.1"\n',
           "docs__a.md": "a, version 2\n",
           "upgrade.sh": "#!/usr/bin/env bash\necho upgrade v2\n",
           "server__smp__migrations__versions__0002_dropped.py": None,
           "server__smp__migrations__versions__0003.py": "three\n",
           # A release must never be able to replace these:
           ".env": "SMP_SECRET_KEY=from-the-release\n",
           "nginx__certs__fullchain.pem": "another cert\n"},
    )
    backup = target / "backups" / "b1"
    lists = install(new, target, tmp_path / "plan2", backup)

    assert lists["added"] == ["server/smp/migrations/versions/0003.py"]
    assert lists["changed"] == ["Version.toml", "docs/a.md", "upgrade.sh"]
    assert lists["removed"] == ["server/smp/migrations/versions/0002_dropped.py"]
    assert lists["modified"] == ["docs/a.md"]

    assert (target / "docs" / "a.md").read_text() == "a, version 2\n"
    assert not (target / "server/smp/migrations/versions/0002_dropped.py").exists()
    assert (target / "server/smp/migrations/versions/0003.py").read_text() == "three\n"
    # untouched
    assert (target / ".env").read_text() == "SMP_SECRET_KEY=keep-me\n"
    assert (target / "nginx/certs/fullchain.pem").read_text() == "pinned cert\n"
    assert (target / "nginx/certs/privkey.pem").read_text() == "key\n"
    assert (target / "my-notes.txt").read_text() == "mine\n"
    assert (target / "backups/old/x").read_text() == "x\n"
    # the backup: previous copies (with the hand edit) and the lists
    assert (backup / "docs/a.md").read_text() == "edited by hand\n"
    assert (backup / "server/smp/migrations/versions/0002_dropped.py").read_text() == "two\n"
    assert (backup / "Version.toml").read_text() == '[release]\nversion = "1.0.0"\n'
    assert (backup / "added-files.txt").read_text() == "server/smp/migrations/versions/0003.py\n"
    assert (backup / "modified-files.txt").read_text() == "docs/a.md\n"
    assert (backup / ".smp-manifest").exists()
    # renamed into place, not overwritten: a running script keeps its old copy
    assert (tmp_path / "running-upgrade.sh").read_text() == BASE["upgrade.sh"]
    assert (target / "upgrade.sh").read_text().endswith("upgrade v2\n")
    # the new manifest describes the new release (protected paths left out)
    manifest = (target / ".smp-manifest").read_text()
    assert "0003.py" in manifest and "0002_dropped.py" not in manifest
    assert ".env" not in manifest.split() and "nginx/certs" not in manifest
    assert not list(target.rglob("*.smp-new"))


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_rollback_puts_the_old_release_back_exactly(tmp_path):
    target = tmp_path / "smp"
    target.mkdir()
    install(release(tmp_path, "r1"), target, tmp_path / "plan1")
    (target / ".env").write_text("SMP_SECRET_KEY=keep-me\n")
    (target / "docs" / "a.md").write_text("edited by hand\n")
    before = snapshot(target)

    new = release(tmp_path, "r2", **{"docs__a.md": "v2\n", "docs__b__new.md": "added\n",
                                     "server__smp__migrations__versions__0002_dropped.py": None})
    backup = target / "backups" / "b1"
    install(new, target, tmp_path / "plan2", backup)
    assert (target / "docs" / "b" / "new.md").exists()

    subprocess.run(["bash", "-c", f'set -euo pipefail; source "{RELEASE_SH}"; smp_restore_files "$1" "$2"', "-",
                    str(backup), str(target)], check=True, capture_output=True, text=True)
    after = {k: v for k, v in snapshot(target).items() if not k.startswith("backups/")}
    assert after == before  # incl. the hand edit, the dropped migration and the old manifest
    assert not (target / "docs" / "b").exists()  # the folder the upgrade added is gone too


def test_rollback_refuses_an_incomplete_backup(tmp_path):
    target = tmp_path / "smp"
    target.mkdir()
    install(release(tmp_path, "r1"), target, tmp_path / "plan1")
    backup = target / "backups" / "b1"
    install(release(tmp_path, "r2", **{"docs__a.md": "v2\n"}), target, tmp_path / "plan2", backup)
    (backup / "docs" / "a.md").unlink()
    out = subprocess.run(["bash", "-c", f'source "{RELEASE_SH}"; smp_restore_files "$1" "$2"', "-", str(backup), str(target)],
                         capture_output=True, text=True)
    assert out.returncode != 0 and "docs/a.md is missing" in out.stderr
    assert (target / "docs" / "a.md").read_text() == "v2\n"  # nothing was touched


def test_without_a_manifest_nothing_is_removed(tmp_path):
    target = make_tree(tmp_path / "smp", BASE)  # e.g. unpacked by hand, before upgrade.sh existed
    lists = install(release(tmp_path, "r2", **{"docs__a.md": None}), target, tmp_path / "plan")
    assert lists["removed"] == [] and (target / "docs/a.md").exists()


def test_running_twice_changes_nothing(tmp_path):
    new, target = release(tmp_path, "r1"), tmp_path / "smp"
    target.mkdir()
    install(new, target, tmp_path / "p1")
    assert install(new, target, tmp_path / "p2") == {"added": [], "changed": [], "removed": [], "modified": []}


def test_symlinks_in_a_release_are_refused(tmp_path):
    new = release(tmp_path, "r1")
    (new / "docs" / "link").symlink_to("/etc/passwd")
    target = tmp_path / "smp"
    target.mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        install(new, target, tmp_path / "plan")
    assert not (target / "Version.toml").exists()


# ---- install.sh --fetch (the download step both scripts use)

def tarball(tmp_path: Path, tree: Path, top: str = "SMP-main") -> Path:
    out = tmp_path / f"{tree.name}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(tree, arcname=top)
    return out


def fetch(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(ROOT / "install.sh"), "--fetch", str(tmp_path / "got"), *args],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_fetch_unpacks_a_release(tmp_path):
    archive = tarball(tmp_path, release(tmp_path, "r1"))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    out = fetch(tmp_path, "--download-url", archive.as_uri(), "--sha256", digest)
    assert out.returncode == 0, out.stderr + out.stdout
    assert (tmp_path / "got" / "Version.toml").read_text() == BASE["Version.toml"]
    assert os.access(tmp_path / "got" / "install.sh", os.X_OK)


def test_fetch_refuses_a_wrong_checksum(tmp_path):
    archive = tarball(tmp_path, release(tmp_path, "r1"))
    out = fetch(tmp_path, "--download-url", archive.as_uri(), "--sha256", "0" * 64)
    assert out.returncode != 0 and "SHA-256" in out.stdout
    assert not (tmp_path / "got").exists()


def test_fetch_refuses_something_that_is_not_smp(tmp_path):
    archive = tarball(tmp_path, make_tree(tmp_path / "other", {"README": "hello\n"}))
    out = fetch(tmp_path, "--download-url", archive.as_uri())
    assert out.returncode != 0 and "not an SMP release" in out.stdout
    assert not (tmp_path / "got").exists()


def test_fetch_refuses_a_release_missing_required_files(tmp_path):
    archive = tarball(tmp_path, release(tmp_path, "r1", **{"server__Dockerfile": None}))
    out = fetch(tmp_path, "--download-url", archive.as_uri())
    assert out.returncode != 0 and "server/Dockerfile is missing" in out.stderr
    assert not (tmp_path / "got").exists()


def test_the_real_tree_is_a_valid_release():
    out = subprocess.run(["bash", "-c", f'source "{RELEASE_SH}"; smp_check_release "{ROOT}"'],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize(
    ("a", "b", "result"),
    [
        ("1.0.2 C", "1.0.3", "lt"),
        ("1.0.3", "1.0.2 C", "gt"),
        ("1.0.2", "1.0.2 A", "lt"),  # a revision letter comes after the plain version
        ("1.0.2 B", "1.0.2 A", "gt"),
        ("1.0.2 C", "1.0.2 C", "eq"),
        ("1.10.0", "1.9.9", "gt"),  # numbers, not text
        ("2.0.0", "10.0.0", "lt"),
    ],
)
def test_version_compare(a, b, result):
    out = subprocess.run(["sh", str(VERSION_SH), "--compare", a, b], capture_output=True, text=True, check=True)
    assert out.stdout == result + "\n"


@pytest.mark.parametrize("bad", ["1.0", "1.0.2C", "v1.0.2", ""])
def test_version_compare_refuses_malformed_versions(bad):
    out = subprocess.run(["sh", str(VERSION_SH), "--compare", bad, "1.0.0"], capture_output=True, text=True)
    assert out.returncode == 1 and "is not a version" in out.stderr


def apply_reports(new: Path, target: Path, plan: Path, backup: Path) -> subprocess.CompletedProcess:
    """smp_apply called the way upgrade.sh calls it (`if ! smp_apply`): there bash ignores
    `set -e` inside the function, so only its own return value tells about failures."""
    script = ('set -euo pipefail; source "$5"; smp_plan "$1" "$2" "$3"; '
              'if smp_apply "$1" "$2" "$3" "$4"; then echo APPLIED; else echo FAILED; fi')
    return subprocess.run(["bash", "-c", script, "-", str(new), str(target), str(plan), str(backup), str(RELEASE_SH)],
                          capture_output=True, text=True)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")
def test_a_failed_copy_is_reported_not_ignored(tmp_path):
    target = tmp_path / "smp"
    target.mkdir()
    install(release(tmp_path, "r1"), target, tmp_path / "plan1")
    r2 = release(tmp_path, "r2", docs__a_md="new a\n", server__Dockerfile="FROM scratch AS two\n")
    untouched = BASE["server/Dockerfile"]

    # A file of the installation that can't be read: refused while planning (it would
    # otherwise look absent and be overwritten without a backup).
    (target / "docs" / "a.md").chmod(0o000)
    try:
        r = apply_reports(r2, target, tmp_path / "plan2", tmp_path / "backup2")
    finally:
        (target / "docs" / "a.md").chmod(0o644)
    assert r.returncode != 0 and "can't read" in r.stderr and "docs/a.md" in r.stderr
    assert (target / "server" / "Dockerfile").read_text() == untouched

    # The backup can't be written: reported before anything is replaced.
    backup = tmp_path / "backup3"
    backup.mkdir()
    backup.chmod(0o555)
    try:
        r = apply_reports(r2, target, tmp_path / "plan3", backup)
    finally:
        backup.chmod(0o755)
    assert r.stdout.strip() == "FAILED" and "couldn't back up" in r.stderr
    assert (target / "server" / "Dockerfile").read_text() == untouched

    # A file can't be written (e.g. a full disk): reported, so upgrade.sh rolls back.
    (target / "server").chmod(0o555)
    try:
        r = apply_reports(r2, target, tmp_path / "plan4", tmp_path / "backup4")
    finally:
        (target / "server").chmod(0o755)
    assert r.stdout.strip() == "FAILED" and "couldn't write server/Dockerfile" in r.stderr
    assert not (target / "server" / "Dockerfile.smp-new").exists()


def version_file(tmp_path, name: str, **versions: str) -> Path:
    """A Version.toml with every section; `versions` overrides (site_blocker -> Site-blocker)."""
    v = {"release": "1.0.3", "server": "1.0.3 B", "WEBUI": "1.0.3 B", "agent": "1.0.3 B", "Site-blocker": "1.0.0 A"}
    v.update({k.replace("site_blocker", "Site-blocker"): val for k, val in versions.items()})
    path = tmp_path / name
    path.write_text("".join(f'[{s}] # comment\nname = "x"\nversion = "{val}"\n\n' for s, val in v.items()))
    return path


def version_diff(old: Path, new: Path) -> list[list[str]]:
    out = subprocess.run(["sh", str(VERSION_SH), "--diff", str(old), str(new)], capture_output=True, text=True, check=True)
    return [line.split("\t") for line in out.stdout.splitlines()]


def test_version_diff_lists_every_component_that_changed(tmp_path):
    """upgrade.sh --force-components: the same release, other component builds."""
    installed = version_file(tmp_path, "old.toml")
    assert version_diff(installed, installed) == []  # nothing differs: up to date
    newer = version_file(tmp_path, "new.toml", server="1.0.3 C", agent="1.0.3 D")
    assert version_diff(installed, newer) == [["server", "1.0.3 B", "1.0.3 C", "up"], ["agent", "1.0.3 B", "1.0.3 D", "up"]]
    older = version_file(tmp_path, "older.toml", site_blocker="0.9.0", release="1.0.3")
    assert version_diff(installed, older) == [["Site-blocker", "1.0.0 A", "0.9.0", "down"]]
    broken = tmp_path / "broken.toml"
    broken.write_text("[server]\nversion = \"1.0.3\"\n")  # sections missing
    r = subprocess.run(["sh", str(VERSION_SH), "--diff", str(installed), str(broken)], capture_output=True, text=True)
    assert r.returncode != 0  # upgrade.sh stops with a message instead of guessing


STATUS_SH = ROOT / "tools" / "smp-status.sh"


def run_status(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, str]:
    """tools/smp-status.sh with a fake `docker` that only records what it was asked."""
    calls = tmp_path / "docker-calls"
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", str(STATUS_SH), *args], capture_output=True, text=True, env=env)
    return r, calls.read_text() if calls.exists() else ""


@pytest.mark.parametrize(
    "args",
    [
        ("logs", "server; id"),  # nothing but the four services
        ("logs", "../../etc"),
        ("logs", "server", "-f"),  # LINES: a number, nothing else
        ("logs", "server", "5000"),
        ("logs", "server", "10", "extra"),
        ("logs",),
        ("agents", "extra"),
        ("exec",),
        ("status", "--format", "x"),
    ],
)
def test_status_script_refuses_anything_unexpected(tmp_path, args):
    """It runs as root through sudo for a monitoring account: arguments are checked
    strictly, and nothing reaches docker unless they pass."""
    r, calls = run_status(tmp_path, *args)
    assert r.returncode == 2 and "usage:" in r.stderr
    assert calls == ""


def test_status_script_runs_only_fixed_read_only_commands(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    r, calls = run_status(tmp_path / "a", "logs", "dns-sinkhole", "50")
    assert r.returncode == 0, r.stderr
    assert calls.strip() == "compose logs --no-color --timestamps --tail 50 dns-sinkhole"
    r, calls = run_status(tmp_path / "b", "agents")
    assert r.returncode == 0, r.stderr
    assert calls.startswith("compose exec -T db psql") and "SELECT" in calls
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", calls)  # reads only
