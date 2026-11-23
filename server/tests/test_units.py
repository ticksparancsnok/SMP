# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Pure unit tests (no database)."""

from datetime import UTC, datetime

import pytest

from smp.core.events import subject_matches
from smp.core.models import Schedule
from smp.core.module import Module, ModuleRegistry, Permission


@pytest.mark.parametrize(
    ("pattern", "subject", "ok"),
    [
        ("core.device.updated", "core.device.updated", True),
        ("core.device.*", "core.device.updated", True),
        ("core.*", "core.device.updated", False),
        ("core.>", "core.device.updated", True),
        ("core.>", "core", False),
        ("dns.>", "core.device.updated", False),
        ("*.device.*", "core.device.deleted", True),
    ],
)
def test_subject_matches(pattern, subject, ok):
    assert subject_matches(pattern, subject) is ok


def _sched(windows):
    return Schedule(name="s", timezone="Europe/Budapest", windows=windows)


def test_schedule_window():
    s = _sched([{"days": [0, 1, 2, 3, 4], "start": "08:00", "end": "08:45"}])
    # 2026-11-23 is a Monday; Budapest is UTC+1 in November.
    assert not _active(s, "2026-11-23T06:59:00")
    assert _active(s, "2026-11-23T07:00:00")
    assert _active(s, "2026-11-23T07:44:00")
    assert not _active(s, "2026-11-23T07:45:00")
    assert not _active(s, "2026-11-22T07:10:00")  # Sunday


def test_schedule_crosses_midnight():
    from smp.core.policy import schedule_active

    s = _sched([{"days": [4], "start": "22:00", "end": "06:00"}])  # Friday night
    assert schedule_active(s, datetime(2026, 11, 27, 22, 30, tzinfo=UTC))  # Fri 23:30 local
    assert schedule_active(s, datetime(2026, 11, 28, 3, 0, tzinfo=UTC))  # Sat 04:00 local
    assert not schedule_active(s, datetime(2026, 11, 28, 6, 0, tzinfo=UTC))  # Sat 07:00 local
    assert not schedule_active(s, datetime(2026, 11, 27, 3, 0, tzinfo=UTC))  # Fri 04:00 local


def _active(s, iso):
    from smp.core.policy import schedule_active

    return schedule_active(s, datetime.fromisoformat(iso + "+00:00"))


def test_registry_rejects_foreign_permission_prefix():
    class Bad(Module):
        name = "bad"
        title = "Bad"
        permissions = (Permission("core.users.write", "steal core perm"),)

    with pytest.raises(ValueError, match="must start with 'bad.'"):
        ModuleRegistry([Bad()])


def test_registry_role_permissions():
    class M(Module):
        name = "mod"
        title = "M"
        permissions = (Permission("mod.a", "a", ("teacher",)), Permission("mod.b", "b"))

    reg = ModuleRegistry([M()])
    assert reg.role_permissions("teacher") == {"mod.a"}
    assert reg.role_permissions("student") == frozenset()
    assert reg.role_permissions("admin") == {"*"}


def test_dns_hosts_parser_normalizes_and_deduplicates():
    from smp.modules.dns.domains import parse_hosts

    content = """# comment
0.0.0.0 Example.COM. ads.example   # inline comment
127.0.0.1 localhost
127.0.0.1 localhost.localdomain local
::1 localhost ip6-localhost ip6-loopback
fe80::1%lo0 localhost link-local.example
ff02::1 ip6-allnodes
255.255.255.255 broadcasthost
0.0.0.0 0.0.0.0
10.0.0.1 one.test two.test
10.0.0.2 one.test
0.0.0.0 a..b -bad.example bad-.example *.wild.example under_score.example
0.0.0.0 ünnep.hu
0.0.0.0 LONGLABEL.example
not-an-ip both.example
""".replace("LONGLABEL", "x" * 64)
    assert parse_hosts(content.encode()) == [
        "example.com",
        "ads.example",
        # localhost.localdomain: two labels, but the machine's own name, not a site
        "one.test",
        "two.test",
        "under_score.example",
        "xn--nnep-zra.hu",  # IDN as punycode, which is what DNS queries carry
    ]


def test_dns_domain_rules():
    from smp.modules.dns.domains import is_public_suffix
    from smp.modules.dns.domains import normalize_domain as nd

    assert nd(" WWW.Example.COM. ") == "www.example.com"
    for bad in ("", ".", "com", "localhost", "a..b", "-x.com", "x-.com", "*.ads.com", "1.2.3.4", "::1",
                "x" * 64 + ".com", ("a" * 60 + ".") * 5 + "com", "with space.com", "slash/x.com"):
        assert nd(bad) is None, bad
    for suffix in ("com", "hu", "co.uk", "co.hu", "org.hu"):
        assert is_public_suffix(suffix), suffix
    for name in ("google.com", "bbc.co.uk", "iskola.hu", "github.io", "blogspot.com"):
        assert not is_public_suffix(name), name


def test_dns_hosts_parser_on_real_list():
    """The StevenBlack hosts file in the repo: every result is well-formed and none
    of its own system entries (localhost, broadcasthost, ip6-*) gets through."""
    from pathlib import Path

    from smp.modules.dns.domains import normalize_domain, parse_hosts

    path = Path(__file__).resolve().parents[2] / "Site-blocker" / "hosts.txt"
    domains = parse_hosts(path.read_bytes())
    assert len(domains) > 70_000 and len(domains) == len(set(domains))
    assert all(normalize_domain(d) == d for d in domains)
    assert not {"localhost", "local", "broadcasthost", "ip6-localhost", "ip6-allnodes", "0.0.0.0"} & set(domains)


def test_dns_decision_order():
    from types import SimpleNamespace as E

    from smp.modules.dns.domains import blocked_by, by_domain

    block = by_domain([E(domain="example.com", include_subdomains=True), E(domain="exact.test", include_subdomains=False),
                       E(domain="co.uk", include_subdomains=True)])  # legacy TLD entry: ignored
    allow = by_domain([E(domain="edu.example.com", include_subdomains=True)])
    assert blocked_by("www.example.com", allow, block).domain == "example.com"
    assert blocked_by("edu.example.com", allow, block) is None  # 1. allowlist wins
    assert blocked_by("x.edu.example.com", allow, block) is None
    assert blocked_by("exact.test", allow, block).domain == "exact.test"  # 2. blocklist
    assert blocked_by("sub.exact.test", allow, block) is None  # not include_subdomains
    assert blocked_by("bbc.co.uk", allow, block) is None  # public-suffix entries never apply
    assert blocked_by("other.org", allow, block) is None  # 3. everything else is allowed
