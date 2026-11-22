# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Integration tests run against a real Postgres (the schema relies on JSONB, NULLS NOT
DISTINCT and a plpgsql trigger). Start one with:

    docker compose up -d db          # from the repo root
    pytest                           # uses 127.0.0.1:5432 by default

Override with SMP_TEST_DATABASE_URL. Only the separate smp_test database is touched.

The test database is dropped and recreated on every run.
"""

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import asyncpg
import httpx
import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import text

TEST_DB_URL = os.environ.get("SMP_TEST_DATABASE_URL", "postgresql+asyncpg://smp:smp@127.0.0.1:5432/smp_test")
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
