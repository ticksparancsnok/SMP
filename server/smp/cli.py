"""Admin CLI.

    smp migrate                       apply DB migrations
    smp bootstrap --org-name "X" ...  create the first org + admin (idempotent per org slug)
    smp create-token --name dns --perm core.policies.read --perm core.devices.read
    smp reencrypt-otps                re-encrypt stored OTPs after rotating SMP_SECRET_KEY
    smp openapi -o ../WEBUI/openapi.json   dump the API schema for the UI
    smp serve                         run the API server
"""

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select


def alembic_config():
    from alembic.config import Config

    from smp.core.config import get_settings

    cfg = Config()
    cfg.set_main_option("script_location", "smp:migrations")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
    return cfg


def cmd_migrate(_args) -> None:
    from alembic import command

    command.upgrade(alembic_config(), "head")


async def _bootstrap(args) -> None:
    from smp.core import db
    from smp.core.models import AuditLog, Organization, Role, RoleAssignment, User
    from smp.core.passwords import password_problem
    from smp.core.security import hash_password

    password = os.environ.get("SMP_BOOTSTRAP_PASSWORD") or getpass.getpass("Admin password: ")
    if problem := password_problem(password, args.admin_username):
        sys.exit(f"password rejected: {problem}")
    db.init_engine()
    async with db.sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.slug == args.org_slug))).scalar_one_or_none()
        if org is None:
            org = Organization(slug=args.org_slug, name=args.org_name, timezone=args.timezone)
            s.add(org)
            await s.flush()
            print(f"created organization {org.slug} ({org.id})")
        user = (
            await s.execute(select(User).where(User.org_id == org.id, User.username == args.admin_username))
        ).scalar_one_or_none()
        if user is not None:
            sys.exit(f"user {args.admin_username!r} already exists in {org.slug}; refusing to overwrite")
        user = User(
            org_id=org.id,
            username=args.admin_username,
            display_name="Administrator",
            password_hash=hash_password(password),
            roles=[RoleAssignment(org_id=org.id, role=Role.ADMIN)],
        )
        s.add(user)
        await s.flush()
        s.add(AuditLog(org_id=org.id, actor_type="system", actor_name="cli", action="core.bootstrap",
                       target_type="user", target_id=str(user.id), details={"username": user.username}))
        await s.commit()
        print(f"created admin user {user.username}")
    await db.dispose_engine()


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
        users = (await s.execute(select(User).where(User.otp_secret.is_not(None)))).scalars().all()
        for u in users:
            new = reencrypt_secret(u.otp_secret)
            if new is None:
                unreadable += 1
            else:
                u.otp_secret = new
                done += 1
        await s.commit()
    await db.dispose_engine()
    print(f"re-encrypted {done} one-time password(s) with the current key")
    if unreadable:
        print(f"{unreadable} could not be decrypted with any configured key: issue new ones for those users")
    else:
        print("you can now remove SMP_SECRET_KEY_PREVIOUS")


def cmd_openapi(args) -> None:
    """Write the OpenAPI schema (used by the web UI to generate TypeScript types)."""
    import json

    from smp.main import create_app

    data = json.dumps(create_app().openapi(), indent=1, ensure_ascii=False)
    if args.output == "-":
        print(data)
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(data + "\n")


def cmd_serve(args) -> None:
    import uvicorn

    from smp.core.config import get_settings

    uvicorn.run(
        "smp.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=get_settings().forwarded_allow_ips,
    )


def main(argv: list[str] | None = None) -> None:
    from smp.core.config import get_settings

    p = argparse.ArgumentParser(prog="smp")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)

    b = sub.add_parser("bootstrap", help="create the first organization and admin user")
    b.add_argument("--org-name", required=True)
    b.add_argument("--org-slug", default=get_settings().default_org)
    b.add_argument("--timezone", default="Europe/Budapest")
    b.add_argument("--admin-username", default="admin")
    b.set_defaults(func=lambda a: asyncio.run(_bootstrap(a)))

    t = sub.add_parser("create-token", help="create an API token for a service")
    t.add_argument("--name", required=True)
    t.add_argument("--perm", action="append", default=[], help="permission code, repeatable")
    t.add_argument("--org-slug", default=get_settings().default_org)
    t.set_defaults(func=lambda a: asyncio.run(_create_token(a)))

    r = sub.add_parser("reencrypt-otps", help="re-encrypt stored one-time passwords after rotating SMP_SECRET_KEY")
    r.set_defaults(func=lambda a: asyncio.run(_reencrypt_otps(a)))

    o = sub.add_parser("openapi", help="dump the OpenAPI schema as JSON")
    o.add_argument("-o", "--output", default="-")
    o.set_defaults(func=cmd_openapi)

    s = sub.add_parser("serve", help="run the API server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
