# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)  # ruff: ignore[missing-copyright-notice]
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Admin CLI.

    smp preflight                     check config + database login, explain what's wrong
    smp status                        is the database reachable, is the school set up?
    smp migrate                       apply DB migrations
    smp dns-sync                      push the DNS block/allowlists to the sinkhole (install.sh does)
    smp bootstrap --org-name "X" ...  create the first org + admin (idempotent per org slug)
                  --otp               ...with a one-time password instead of a typed one
    smp create-token --name dns --perm core.policies.read --perm core.devices.read
    smp reencrypt-otps                re-encrypt stored OTPs after rotating SMP_SECRET_KEY
    smp backup encrypt|decrypt|verify database dumps with SMP_BACKUP_KEY, stdin -> stdout
    smp openapi -o ../WEBUI/openapi.json   dump the API schema for the UI
    smp serve                         run the API server
"""  # ruff: ignore[line-too-long, over-indentation]

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import pathlib
import sys
import time

from alembic.config import Config  # ruff: ignore[typing-only-third-party-import]
from sqlalchemy import select


def alembic_config() -> Config:  # ruff: ignore[undocumented-public-function]
    from alembic.config import Config  # ruff: ignore[import-outside-top-level]

    from smp.core.config import get_settings  # ruff: ignore[import-outside-top-level]

    cfg = Config()
    cfg.set_main_option("script_location", "smp:migrations")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))  # ruff: ignore[line-too-long]
    return cfg


def cmd_migrate(_args) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]  # ruff: ignore[missing-type-function-argument, undocumented-public-function]
    from alembic import command  # ruff: ignore[import-outside-top-level]

    command.upgrade(alembic_config(), "head")


# bootstrap: the admin user exists already (install.sh relies on it)
EXIT_ALREADY_EXISTS = 3


async def _bootstrap(args) -> None:  # ruff: ignore[complex-structure, too-many-branches, too-many-statements]
    """Create the school (organization) and its first admin.

    --otp: the admin gets a one-time password (printed once) and must choose their own
    at first login, like every other account someone else sets up. Otherwise the
    password is typed here (or SMP_BOOTSTRAP_PASSWORD) and kept.
    --machine: print KEY=value lines (for install.sh) instead of sentences.
    """
    from datetime import timedelta  # ruff: ignore[import-outside-top-level]

    from smp.core import db  # ruff: ignore[import-outside-top-level]
    from smp.core.core_module import CoreSettings  # ruff: ignore[import-outside-top-level]
    from smp.core.db import utcnow  # ruff: ignore[import-outside-top-level]
    from smp.core.models import (  # ruff: ignore[import-outside-top-level]
        AuditLog,
        ModuleSetting,
        Organization,
        Role,
        RoleAssignment,
        User,
    )
    from smp.core.otp import set_otp  # ruff: ignore[import-outside-top-level]
    from smp.core.passwords import password_problem  # ruff: ignore[import-outside-top-level]
    from smp.core.security import (  # ruff: ignore[import-outside-top-level]
        SecretKeyMissing,
        generate_otp,
        hash_otps,
        hash_password,
        require_secret_key,
    )

    name = args.org_name.strip()
    if not name:
        sys.exit("the school name can't be empty")
    password = None
    if args.otp:
        try:
            require_secret_key()  # the OTP is stored encrypted for the login slips
        except SecretKeyMissing as e:
            sys.exit(str(e))
    else:
        password = os.environ.get("SMP_BOOTSTRAP_PASSWORD") or getpass.getpass("Admin password: ")  # ruff: ignore[line-too-long]
        if problem := password_problem(password, args.admin_username):
            sys.exit(f"password rejected: {problem}")
    db.init_engine()
    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.slug == args.org_slug))).scalar_one_or_none()  # ruff: ignore[line-too-long]
        if org is None:
            org = Organization(slug=args.org_slug, name=name, timezone=args.timezone)  # ruff: ignore[line-too-long]
            s.add(org)
            await s.flush()
            # Keep the "school name" setting (Settings page) in step with the organization.  # ruff: ignore[line-too-long]
            s.add(ModuleSetting(org_id=org.id, module="core", data=CoreSettings(school_name=name).model_dump(mode="json")))  # ruff: ignore[line-too-long]
            if not args.machine:
                print(f"created organization {org.slug}: {org.name}")  # ruff: ignore[print]
        user = (
            await s.execute(select(User).where(User.org_id == org.id, User.username == args.admin_username))  # ruff: ignore[line-too-long]
        ).scalar_one_or_none()
        if user is not None:
            print(f"user {args.admin_username!r} already exists in {org.slug}; refusing to overwrite", file=sys.stderr)  # ruff: ignore[line-too-long, print]
            sys.exit(EXIT_ALREADY_EXISTS)
        user = User(
            org_id=org.id,
            username=args.admin_username,
            display_name="Administrator",
            roles=[RoleAssignment(org_id=org.id, role=Role.ADMIN)],
        )
        otp = None
        if args.otp:
            otp = generate_otp()
            (digest,) = hash_otps([otp])
            set_otp(user, otp, digest, utcnow() + timedelta(days=CoreSettings().otp_valid_days))
        else:
            user.password_hash = hash_password(password)  # chosen here, so no forced change
        s.add(user)
        await s.flush()
        s.add(AuditLog(org_id=org.id, actor_type="system", actor_name="cli", action="core.bootstrap",
                       target_type="user", target_id=str(user.id),
                       details={"username": user.username, "one_time_password": otp is not None}))
        await s.commit()
        until = user.otp_expires_at.strftime("%Y-%m-%d %H:%M UTC") if user.otp_expires_at else ""
    await db.dispose_engine()
    if args.machine:
        print(f"SMP_ADMIN_USERNAME={user.username}")
        print(f"SMP_SCHOOL_NAME={org.name}")
        if otp:
            print(f"SMP_ADMIN_OTP={otp}")
            print(f"SMP_ADMIN_OTP_VALID_UNTIL={until}")
    else:
        print(f"created admin user {user.username}")
        if otp:
            print(f"one-time password: {otp} (valid until {until}; must be changed at first login)")


async def _status(args) -> None:
    """KEY=value lines: database reachable? school set up? Exit 1 if the DB is unreachable."""  # ruff: ignore[line-too-long]
    from sqlalchemy import func, text

    from smp.core import db
    from smp.core.models import Organization, Role, RoleAssignment

    db.init_engine()
    try:
        async with db.sessionmaker()() as s:
            await s.execute(text("SELECT 1"))
            org = (await s.execute(select(Organization).where(Organization.slug == args.org_slug))).scalar_one_or_none()
            admins = 0
            if org is not None:
                admins = await s.scalar(
                    select(func.count()).where(RoleAssignment.org_id == org.id, RoleAssignment.role == Role.ADMIN)
                ) or 0
    except Exception as e:  # ruff: ignore[blind-except]
        print("database=error")  # ruff: ignore[print]
        print(f"error={type(e).__name__}")  # ruff: ignore[print]
        sys.exit(1)
    finally:
        await db.dispose_engine()
    print("database=ok")  # ruff: ignore[print]
    print(f"bootstrapped={'yes' if org is not None and admins else 'no'}")  # ruff: ignore[print]
    print(f"school_name={org.name if org else ''}")  # ruff: ignore[print]
    print(f"admins={admins}")  # ruff: ignore[print]


async def _dns_sync(args) -> None:
    """Push the school's DNS lists to the sinkhole. The server pushes after every change.

    This covers the rest (a new or emptied sinkhole volume, a restored database).
    """  # ruff: ignore[line-too-long]
    import httpx

    from smp.core import db
    from smp.core.config import get_settings
    from smp.core.models import Organization
    from smp.modules.dns.api import push_to_sinkhole, sync_payload

    db.init_engine()
    try:
        async with db.sessionmaker()() as s:
            org = (await s.execute(select(Organization).where(Organization.slug == args.org_slug))).scalar_one_or_none()
            if org is None:
                print("dns-sync: school not set up yet, nothing to sync")  # ruff: ignore[print]
                return
            payload = await sync_payload(s, org.id)
    finally:
        await db.dispose_engine()
    url = get_settings().dns_sinkhole_url
    try:
        await push_to_sinkhole(payload)
    except httpx.HTTPError as e:
        sys.exit(f"dns-sync: the DNS sinkhole at {url} didn't take the lists ({type(e).__name__}: {e})")  # ruff: ignore[line-too-long]
    print(f"dns-sync: {payload.entries} blocked and {payload.allowlist} allowed domain(s) sent to {url}")  # ruff: ignore[line-too-long, print]


def _preflight_problems() -> list[tuple[str, str]]:
    """Get information for every configuration problem that stops the server.

    Returns:
        list[tuple[str, str]]: (what, how to fix)

    """
    import asyncpg
    from sqlalchemy.engine import make_url

    from smp.core.config import get_settings

    settings = get_settings()
    problems: list[tuple[str, str]] = []
    if p := settings.secret_key_problem():
        problems.append((
            p,
            'Generate one with  python3 -c "import secrets; print(secrets.token_urlsafe(32))"  and set\n'  # ruff: ignore[implicit-string-concatenation-in-collection-literal, line-too-long]
            "SMP_SECRET_KEY=<that value> in .env (./install.sh does this for you), then\n"  # ruff: ignore[line-too-long]
            "docker compose up -d",
        ))

    url = make_url(settings.database_url)
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)

    async def connect() -> None:
        conn = await asyncpg.connect(dsn, timeout=5)
        await conn.close()

    for attempt in range(5):  # the database may still be starting
        try:
            asyncio.run(connect())
            break
        except (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError):  # ruff: ignore[line-too-long]
            problems.append((
                f"the database refused the login for user {url.username!r} (wrong password)",  # ruff: ignore[line-too-long]
                "Postgres sets its password only when the database volume is first created, so\n"  # ruff: ignore[implicit-string-concatenation-in-collection-literal, line-too-long]
                "SMP_DB_PASSWORD in .env must still be the value it had back then (e.g. .env was\n"  # ruff: ignore[line-too-long]
                "regenerated, or copied again from .env.example). Either:\n"
                "  - put the original SMP_DB_PASSWORD back into .env, or\n"
                "  - start over, DELETING ALL DATA:  docker compose down -v  then  ./install.sh",  # ruff: ignore[line-too-long]
            ))
            break
        except asyncpg.InvalidCatalogNameError:
            problems.append((f"the database {url.database!r} doesn't exist", "Check SMP_DATABASE_URL."))  # ruff: ignore[line-too-long]
            break
        except (OSError, TimeoutError, asyncpg.CannotConnectNowError) as e:
            if attempt == 4:
                problems.append((
                    f"can't reach the database at {url.host}:{url.port or 5432} ({type(e).__name__})",  # ruff: ignore[line-too-long]
                    "Is the db container running?  docker compose ps",
                ))
            else:
                time.sleep(2)
    return problems


def cmd_preflight(args) -> None:
    """Run before migrate/serve in the container. A wrong .env otherwise shows up as a
    container that restarts every few seconds with a traceback in its log. Instead:
    say what's wrong and how to fix it, then wait before exiting so the restart loop is
    slow and the message stays readable (docker compose logs server)."""
    problems = _preflight_problems()
    if not problems:
        print("preflight: configuration and database login OK", flush=True)
        return
    bar = "=" * 78
    lines = [bar, "SMP can't start:", ""]
    for i, (what, fix) in enumerate(problems, 1):
        lines += [f"{i}. {what}", *("   " + line for line in fix.splitlines()), ""]
    lines += [f"(waiting {args.fail_delay}s before exiting, so this message isn't lost in a restart loop)", bar]
    print("\n".join(lines), file=sys.stderr, flush=True)
    time.sleep(args.fail_delay)
    sys.exit(1)


async def _create_token(args) -> None:
    from smp.core import db
    from smp.core.models import ApiToken, AuditLog, Organization
    from smp.core.security import new_token
    from smp.modules import build_registry

    known = build_registry().permissions
    if unknown := set(args.perm) - set(known):
        sys.exit(f"unknown permissions: {sorted(unknown)}")
    db.init_engine()
    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.slug == args.org_slug))).scalar_one_or_none()
        if org is None:
            sys.exit(f"no organization {args.org_slug!r}; run bootstrap first")
        plain, digest = new_token("smpt")
        t = ApiToken(org_id=org.id, name=args.name, prefix=plain[:12], token_hash=digest, permissions=sorted(set(args.perm)))
        s.add(t)
        await s.flush()
        s.add(AuditLog(org_id=org.id, actor_type="system", actor_name="cli", action="core.token.create",
                       target_type="apitoken", target_id=str(t.id), details={"name": t.name, "permissions": t.permissions}))
        await s.commit()
    await db.dispose_engine()
    print(plain)


async def _reencrypt_otps(_args) -> None:
    """After rotating SMP_SECRET_KEY (old key in SMP_SECRET_KEY_PREVIOUS): re-encrypt
    every stored one-time password with the new key."""
    from smp.core import db
    from smp.core.config import get_settings
    from smp.core.models import User
    from smp.core.security import reencrypt_secret

    if problem := get_settings().secret_key_problem():
        sys.exit(problem)
    db.init_engine()
    done = unreadable = 0
    async with db.sessionmaker()() as s:
        users = (await s.execute(
            select(User).where(User.otp_secret.is_not(None))
        )).scalars().all()
        for u in users:
            new = reencrypt_secret(u.otp_secret)
            if new is None:
                unreadable += 1
            else:
                u.otp_secret = new
                done += 1
        await s.commit()
    await db.dispose_engine()
    print(f"re-encrypted {done} one-time password(s) with the current key")  # ruff: ignore[print]
    if unreadable:
        print(f"{unreadable} could not be decrypted with any configured key: issue new ones for those users")  # ruff: ignore[line-too-long, print]
    else:
        print("you can now remove SMP_SECRET_KEY_PREVIOUS")  # ruff: ignore[print]


def cmd_openapi(args) -> None:
    """Write the OpenAPI schema (used by the web UI to generate TypeScript types)."""  # ruff: ignore[line-too-long]
    import json  # ruff: ignore[import-outside-top-level]

    from smp.main import create_app  # ruff: ignore[import-outside-top-level]

    data = json.dumps(create_app().openapi(), indent=1, ensure_ascii=False)
    if args.output == "-":
        print(data)  # ruff: ignore[print]
        return

    pathlib.Path(args.output).write_text(data + "\n", encoding="utf-8")


def cmd_serve(args) -> None:  # ruff: ignore[undocumented-public-function]
    import uvicorn  # ruff: ignore[import-outside-top-level]

    from smp.core.config import get_settings  # ruff: ignore[import-outside-top-level]

    uvicorn.run(
        "smp.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=get_settings().forwarded_allow_ips,
    )


def cmd_backup(args) -> None:
    """Stdin -> stdout, for pipes like `pg_dump -Fc | smp backup encrypt > file` (see smp/core/backup.py and tools/db-backup.sh)."""  # ruff: ignore[line-too-long]
    from smp.core.backup import BackupError, decrypt, encrypt  # ruff: ignore[import-outside-top-level]
    from smp.core.config import get_settings  # ruff: ignore[import-outside-top-level]

    settings = get_settings()
    if problem := settings.backup_key_problem():
        sys.exit(f"smp backup: {problem}")
    if args.action != "verify" and sys.stdout.isatty():
        sys.exit("smp backup: the output is binary; redirect it to a file or a pipe")  # ruff: ignore[line-too-long]
    try:
        if args.action == "encrypt":
            encrypt(sys.stdin.buffer, sys.stdout.buffer, settings.backup_key)
        elif args.action == "decrypt":
            decrypt(sys.stdin.buffer, sys.stdout.buffer, settings.backup_key)
        else:
            size = decrypt(sys.stdin.buffer, None, settings.backup_key)
            print(f"backup ok: {size} bytes of database dump")  # ruff: ignore[print]
        sys.stdout.flush()
    except BackupError as e:
        sys.exit(f"smp backup: {e}")


def main(argv: list[str] | None = None) -> None:  # ruff: ignore[undocumented-public-function]
    from smp.core.config import get_settings  # ruff: ignore[import-outside-top-level]

    p = argparse.ArgumentParser(prog="smp")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)  # ruff: ignore[line-too-long]

    b = sub.add_parser("bootstrap", help="create the first organization and admin user")  # ruff: ignore[line-too-long]
    b.add_argument("--org-name", required=True)
    b.add_argument("--org-slug", default=get_settings().default_org)
    b.add_argument("--timezone", default="Europe/Budapest")
    b.add_argument("--admin-username", default="admin")
    b.add_argument("--otp", action="store_true", help="give the admin a one-time password (changed at first login)")  # ruff: ignore[line-too-long]
    b.add_argument("--machine", action="store_true", help="print KEY=value lines instead of sentences")  # ruff: ignore[line-too-long]
    b.set_defaults(func=lambda a: asyncio.run(_bootstrap(a)))

    st = sub.add_parser("status", help="database reachable? school set up? (KEY=value lines)")  # ruff: ignore[line-too-long]
    st.add_argument("--org-slug", default=get_settings().default_org)
    st.set_defaults(func=lambda a: asyncio.run(_status(a)))

    ds = sub.add_parser("dns-sync", help="push the DNS block/allowlists to the sinkhole")  # ruff: ignore[line-too-long]
    ds.add_argument("--org-slug", default=get_settings().default_org)
    ds.set_defaults(func=lambda a: asyncio.run(_dns_sync(a)))

    pf = sub.add_parser("preflight", help="check configuration and database login; explain problems")  # ruff: ignore[line-too-long]
    pf.add_argument("--fail-delay", type=int, default=30, help="seconds to wait before exiting on failure")  # ruff: ignore[line-too-long]
    pf.set_defaults(func=cmd_preflight)

    t = sub.add_parser("create-token", help="create an API token for a service")
    t.add_argument("--name", required=True)
    t.add_argument("--perm", action="append", default=[], help="permission code, repeatable")  # ruff: ignore[line-too-long]
    t.add_argument("--org-slug", default=get_settings().default_org)
    t.set_defaults(func=lambda a: asyncio.run(_create_token(a)))

    r = sub.add_parser("reencrypt-otps", help="re-encrypt stored one-time passwords after rotating SMP_SECRET_KEY")  # ruff: ignore[line-too-long]
    r.set_defaults(func=lambda a: asyncio.run(_reencrypt_otps(a)))

    bk = sub.add_parser("backup", help="encrypt/decrypt a database dump with SMP_BACKUP_KEY (stdin -> stdout)")  # ruff: ignore[line-too-long]
    bk.add_argument("action", choices=["encrypt", "decrypt", "verify"])
    bk.set_defaults(func=cmd_backup)

    o = sub.add_parser("openapi", help="dump the OpenAPI schema as JSON")
    o.add_argument("-o", "--output", default="-")
    o.set_defaults(func=cmd_openapi)

    s = sub.add_parser("serve", help="run the API server")
    s.add_argument("--host", default="0.0.0.0")  # ruff: ignore[hardcoded-bind-all-interfaces]
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
