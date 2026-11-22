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
    from smp.modules.dns.api import parse_hosts

    content = b"""# comment
0.0.0.0 Example.COM. ads.example
127.0.0.1 localhost
::1 localhost
255.255.255.255 broadcasthost
10.0.0.1 one.test two.test
10.0.0.2 one.test
"""
    assert parse_hosts(content) == [
        "example.com",
        "ads.example",
        "one.test",
        "two.test",
    ]
