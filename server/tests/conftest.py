# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Integration tests run against a real Postgres (the schema relies on JSONB, NULLS NOT
DISTINCT and a plpgsql trigger). Start one with:

    docker compose up -d db          # from the repo root
    pytest                           # uses 127.0.0.1:5432 by default

Override with SMP_TEST_DATABASE_URL. Only the separate smp_test database is touched
(smp_test_gw0, smp_test_gw1, ... when run in parallel with `pytest -n auto`).

The test database is dropped and recreated on every run.
"""

import asyncio
import os
import threading
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import asyncpg
import httpx
import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select, text

TEST_DB_URL = os.environ.get("SMP_TEST_DATABASE_URL", "postgresql+asyncpg://smp:smp@127.0.0.1:5432/smp_test")
# `pytest -n auto` (pytest-xdist): every worker gets its own database, smp_test_gw0, ...
if worker := os.environ.get("PYTEST_XDIST_WORKER"):
    TEST_DB_URL = urlsplit(TEST_DB_URL)._replace(path=f"{urlsplit(TEST_DB_URL).path}_{worker}").geturl()
os.environ.update(
    SMP_DATABASE_URL=TEST_DB_URL,
    SMP_COOKIE_SECURE="false",
    SMP_SECRET_KEY="test-secret-key-0123456789-abcdefghijklmnop",
    SMP_DEFAULT_ORG="default",
)

from smp.core import db  # noqa: E402
from smp.core.config import get_settings  # noqa: E402
from smp.core.events import Event, InProcessEventBus  # noqa: E402
from smp.core.models import Organization, Role, RoleAssignment, User  # noqa: E402
from smp.core.module import EventSubject, Module, ModuleRegistry  # noqa: E402
from smp.core.security import hash_password  # noqa: E402
from smp.main import create_app  # noqa: E402
from smp.modules import all_modules  # noqa: E402

get_settings.cache_clear()

ADMIN_PW = "admin-password"
INITIAL_PW = "kezdo-Jelszo-77"  # set by the admin in login_as()
OWN_PW = "sajat-Jelszo-88"  # what the user changes it to


class TestPolicy(BaseModel):
    blocked: list[str] = []


class PolicyTestModule(Module):
    """A fake module that defines a policy kind, as the DNS module will."""

    name = "testpol"
    title = "Test policies"
    policy_model = TestPolicy
    events = (EventSubject("testpol.dummy", "unused"),)

    def __init__(self) -> None:
        self.received: list[Event] = []

    def subscribe(self, bus) -> None:
        async def handler(ev: Event) -> None:
            self.received.append(ev)

        bus.subscribe("core.>", handler)


def _recreate_db() -> None:
    u = urlsplit(TEST_DB_URL.replace("+asyncpg", ""))
    name = u.path.lstrip("/")
    admin_dsn = u._replace(path="/postgres").geturl()

    async def go():
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    asyncio.run(go())

    from alembic import command

    from smp.cli import alembic_config

    command.upgrade(alembic_config(), "head")


@pytest.fixture(scope="session")
def migrated_db():
    # Alembic's env.py calls asyncio.run(), so run it outside pytest's event loop.
    err: list[BaseException] = []

    def target():
        try:
            _recreate_db()
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=target)
    t.start()
    t.join()
    if err:
        raise err[0]


@pytest.fixture(autouse=True)
def no_login_penalty(monkeypatch):
    """Failed logins are padded to LOGIN_PENALTY_SECONDS (1 s) so their timing reveals
    nothing (issue #4). Tests fail logins on purpose all the time, so skip the wait here;
    tests/test_login_timing.py turns it back on to test the padding itself."""
    from smp.core.api import auth

    monkeypatch.setattr(auth, "LOGIN_PENALTY_SECONDS", 0.0)


@pytest.fixture(scope="session")
def testpol() -> PolicyTestModule:
    return PolicyTestModule()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def app(migrated_db, testpol):
    application = create_app(registry=ModuleRegistry([*all_modules(), testpol]), bus=InProcessEventBus())
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture(loop_scope="session")
async def org(app, testpol):
    """Fresh data for every test: one org with an admin user."""
    # Event handlers from the previous test (e.g. the DNS re-sync after a change) run as
    # background tasks and may still be reading the tables; TRUNCATE would deadlock with them.
    await app.state.bus.drain()
    # Classroom watches are in-memory too: one left over from an earlier test times out 20 s
    # later, and the pruner then writes an audit entry whenever that is (mid-TRUNCATE: deadlock).
    from smp.modules.classroom.watch import FrameStore, WatchRegistry

    classroom = app.state.registry.get("classroom")
    classroom.watches, classroom.frames = WatchRegistry(), FrameStore()
    async with db.sessionmaker()() as s:
        await s.execute(text("TRUNCATE organizations CASCADE"))
        o = Organization(slug="default", name="Test School")
        s.add(o)
        await s.flush()
        s.add(
            User(
                org_id=o.id,
                username="admin",
                password_hash=hash_password(ADMIN_PW),
                roles=[RoleAssignment(org_id=o.id, role=Role.ADMIN)],
            )
        )
        await s.commit()
    testpol.received.clear()
    app.state.login_throttle.reset()  # in-memory, so it would leak between tests
    return o


def make_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture(loop_scope="session")
async def admin(app, org):
    async with make_client(app) as c:
        r = await c.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW})
        assert r.status_code == 200, r.text
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def anon(app, org):
    async with make_client(app) as c:
        yield c


@asynccontextmanager
async def login_as(app, admin: httpx.AsyncClient, username: str, roles: list[dict]):
    """Create a user through the API and yield a logged-in client for them."""
    r = await admin.post("/api/v1/users", json={"username": username, "password": INITIAL_PW, "roles": roles})
    assert r.status_code == 201, r.text
    async with make_client(app) as c:
        r = await c.post("/api/v1/auth/login", json={"username": username, "password": INITIAL_PW})
        assert r.status_code == 200, r.text
        # Admin-set passwords must be replaced at first login.
        assert r.json()["must_change_password"] is True
        r = await c.post("/api/v1/auth/password", json={"current_password": INITIAL_PW, "new_password": OWN_PW})
        assert r.status_code == 204, r.text
        yield c


async def make_building(name: str, rooms: list[str] = ()) -> tuple[str, list[str]]:
    """Create a building (and rooms in it) directly in the DB: (building id, room ids)."""
    from smp.core.models import Building, Room

    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization))).scalar_one()
        b = Building(org_id=org.id, name=name)
        s.add(b)
        await s.flush()
        room_rows = [Room(org_id=org.id, name=r, building_id=b.id) for r in rooms]
        s.add_all(room_rows)
        await s.commit()
        return str(b.id), [str(r.id) for r in room_rows]


@asynccontextmanager
async def login_scoped(app, username: str, roles: list[dict]):
    """Create a user directly in the DB with any role assignments, including the
    building-scoped ones the users API may not hand out to the caller ({"role":
    "manager", "building_id": ...}), and yield a logged-in client for them."""
    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization))).scalar_one()
        s.add(
            User(
                org_id=org.id,
                username=username,
                password_hash=hash_password(OWN_PW),
                roles=[
                    RoleAssignment(
                        org_id=org.id,
                        role=r["role"],
                        room_id=uuid.UUID(r["room_id"]) if r.get("room_id") else None,
                        building_id=uuid.UUID(r["building_id"]) if r.get("building_id") else None,
                    )
                    for r in roles
                ],
            )
        )
        await s.commit()
    async with make_client(app) as c:
        r = await c.post("/api/v1/auth/login", json={"username": username, "password": OWN_PW})
        assert r.status_code == 200, r.text
        yield c
