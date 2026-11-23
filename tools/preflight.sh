# shellcheck shell=bash
# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

# Checks before building; sourced (bash) by install.sh and upgrade.sh.
#
#   smp_check_registries   can a container (like the build's) reach the package
#                          registries a rebuild downloads from? Prints what it can't
#                          reach and returns 1 if anything is unreachable.
#
# Why: a rebuild downloads whatever changed since the last build (Python packages from
# PyPI, also setuptools itself for every server build; npm packages for the web UI;
# Rust crates for the agent; Go modules for the DNS filter). Without internet access
# (DNS, firewall, proxy) the build fails halfway; upgrade.sh checks first, so it stops
# before changing anything instead of failing and rolling back.

SMP_CHECK_IMAGE=python:3.13-slim  # the server's base image: already here after a build

smp_check_registries() {
    if ! docker image inspect "$SMP_CHECK_IMAGE" >/dev/null 2>&1 \
        && ! docker pull -q "$SMP_CHECK_IMAGE" </dev/null >/dev/null 2>&1; then
        echo "  can't download $SMP_CHECK_IMAGE from Docker Hub either" >&2
        return 1
    fi
    # Any HTTP answer counts (even 404): the point is that the name resolves and a
    # connection gets through, from a container on Docker's network like the build's.
    docker run --rm -i "$SMP_CHECK_IMAGE" python - <<'EOF'
import sys
import urllib.error
import urllib.request

REGISTRIES = [
    ("Python packages (PyPI)", "https://pypi.org/simple/setuptools/"),
    ("Python package files", "https://files.pythonhosted.org/"),
    ("npm packages (web UI)", "https://registry.npmjs.org/"),
    ("Rust crates (agent)", "https://index.crates.io/config.json"),
    ("Rust crate files", "https://static.crates.io/"),
    ("Go modules (DNS filter)", "https://proxy.golang.org/"),
]
failed = 0
for name, url in REGISTRIES:
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=15)
    except urllib.error.HTTPError:
        pass  # it answered: reachable
    except Exception as e:  # noqa: BLE001 - name resolution, connection, TLS, timeout
        print(f"  {name}: {url} is not reachable ({getattr(e, 'reason', e)})")
        failed += 1
sys.exit(1 if failed else 0)
EOF
}
