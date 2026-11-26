# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)  # ruff: ignore[missing-copyright-notice, undocumented-public-module]
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

import asyncio
import logging
import time
import uuid
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # FastAPI resolves endpoint parameter types at runtime
)

from smp.core.config import get_settings
from smp.core.context import (
    Ctx,
    check_origin,
    client_ip,
    get_ctx,
    principal_for_user,
)
from smp.core.db import get_session, utcnow
from smp.core.errors import AppError
from smp.core.models import (
    AuditLog,
    AuthSession,
    AuthSource,
    Organization,
    User,
)
from smp.core.otp import clear_otp
from smp.core.passwords import password_problem
from smp.core.security import (
    hash_password_async,
    hash_token,
    new_token,
    password_needs_rehash,
    verify_password_async,
)
from smp.core.throttle import LoginThrottle

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger(__name__)


class LoginIn(BaseModel):  # ruff: ignore[undocumented-public-class]
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    org: str | None = None  # org slug; defaults to SMP_DEFAULT_ORG


# The rows-per-page choices the UI offers (WEBUI/src/paging.ts PAGE_SIZES).
PageSize = Literal[10, 50, 100, 500]

# The UI languages (WEBUI/src/i18n/index.ts LOCALES).
Locale = Literal["hu", "en"]

# A list's key in the UI, e.g. "dns.blocklist" or "users".
PrefKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.-]{1,64}$")]

# Far more lists than the UI has; keeps the stored JSON small
MAX_PAGE_SIZE_KEYS = 100


class PreferencesOut(BaseModel):
    """A user's own UI settings, stored on their account (users.preferences)."""  # ruff: ignore[line-too-long]

    page_size: dict[str, PageSize] = {}
    # None = not chosen on this account yet (the UI then keeps the browser's choice).  # ruff: ignore[line-too-long]
    locale: Locale | None = None


class PreferencesPatch(BaseModel):
    """Merged into the stored preferences: only the fields sent change. A null page size
    removes that list's choice; "locale": null forgets the language.
    """  # ruff: ignore[line-too-long, missing-blank-line-after-summary]

    model_config = ConfigDict(extra="forbid")

    page_size: dict[PrefKey, PageSize | None] = Field(default={}, max_length=MAX_PAGE_SIZE_KEYS)  # ruff: ignore[line-too-long]
    locale: Locale | None = None


def _preferences(user: User | None) -> PreferencesOut:
    # Unknown or no-longer-valid stored values are dropped rather than failing /auth/me.  # ruff: ignore[line-too-long]
    raw = (user.preferences if user else None) or {}
    sizes = raw.get("page_size") if isinstance(raw.get("page_size"), dict) else {}
    locale = raw.get("locale") if raw.get("locale") in {"hu", "en"} else None
    return PreferencesOut(page_size={k: v for k, v in sizes.items() if v in {10, 50, 100, 500}}, locale=locale)  # ruff: ignore[line-too-long]


class MeOut(BaseModel):  # ruff: ignore[undocumented-public-class]
    id: uuid.UUID
    kind: str
    username: str
    display_name: str
    org_id: uuid.UUID
    org_name: str
    roles: list[dict[str, str | None]]
    permissions: list[str]
    room_permissions: dict[str, list[str]]
    # Building-level permissions per building (building-scoped roles only), and which
    # building each room of those buildings is in (room id -> building id).
    building_permissions: dict[str, list[str]] = {}
    room_buildings: dict[str, str] = {}
    must_change_password: bool = False
    preferences: PreferencesOut = PreferencesOut()


class PasswordChangeIn(BaseModel):  # ruff: ignore[undocumented-public-class]
    current_password: str
    new_password: str = Field(min_length=8, max_length=1024)


def _audit_login(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    db: AsyncSession,
    request: Request,
    org_id,
    user: User | None,
    username: str,
    ok: bool,
    reason=""
) -> None:
    db.add(
        AuditLog(
            org_id=org_id,
            actor_type="user" if user else "anonymous",
            actor_id=str(user.id) if user else None,
            actor_name=username,
            action="core.auth.login" if ok else "core.auth.login_failed",
            target_type="user" if user else None,
            target_id=str(user.id) if user else None,
            details={"reason": reason} if reason else {},
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:400] or None,
        )
    )


async def _start_session(db: AsyncSession, request: Request, response: Response, user: User) -> None:
    """Create a session row and set its cookie (the caller commits)."""
    settings = get_settings()
    token, token_hash = new_token("smps")
    db.add(
        AuthSession(
            org_id=user.org_id,
            user_id=user.id,
            token_hash=token_hash,
            expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:400] or None,
        )
    )
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


# How many seconds it takes to send a response for any kind of invalid login.
# This shoud be slower successful login on purpose.
# All invalid paths login can take should take the same time.
LOGIN_PENALTY_SECONDS: float = 1.0


@router.post("/login", response_model=MeOut)
async def login(body: LoginIn, request: Request, response: Response, db: Annotated[AsyncSession, Depends(get_session)]) -> MeOut:  # ruff: ignore[line-too-long, undocumented-public-function]
    deadline = time.perf_counter() + LOGIN_PENALTY_SECONDS

    settings = get_settings()
    registry = request.app.state.registry
    throttle: LoginThrottle = request.app.state.login_throttle
    bad = AppError(status.HTTP_401_UNAUTHORIZED, "auth.invalid_credentials", "invalid username or password")  # ruff: ignore[line-too-long]
    ip = client_ip(request) or "unknown"

    # Checked before any DB work or hashing, and identical for existing and unknown  # ruff: ignore[line-too-long]
    # usernames, so it's no enumeration oracle. A locked account refuses even the  # ruff: ignore[line-too-long]
    # right password (see smp/core/throttle.py for the lockout trade-off).
    if not throttle.allowed(ip, body.username):
        await asyncio.sleep(deadline - time.perf_counter())
        raise AppError(status.HTTP_429_TOO_MANY_REQUESTS, "auth.too_many_attempts", "too many login attempts, try again later")  # ruff: ignore[line-too-long]
    throttle.record_attempt(ip)

    org = (
        await db.execute(select(Organization).where(Organization.slug == (body.org or settings.default_org)))
    ).scalar_one_or_none()
    if org is None:
        throttle.record_failure(body.username)
        await asyncio.sleep(deadline - time.perf_counter())
        raise bad
    user = (
        await db.execute(select(User).where(User.org_id == org.id, User.username == body.username))
    ).scalar_one_or_none()

    now = utcnow()
    ok, reason = False, "unknown or disabled user"
    if (user is not None) and (not user.disabled):
        provider = request.app.state.identity_providers.get(user.auth_source)
        ok = provider is not None and await provider.verify_password(user, body.password)
        reason = "bad password"
        if ok and user.otp_expires_at is not None and user.otp_expires_at <= now:
            ok, reason = False, "one-time password expired"

    if not ok:
        throttle.record_failure(body.username)
        _audit_login(db, request, org.id, user, body.username, False, reason)
        try:
            await db.commit()
        except Exception:
            # Never let a DB hiccup turn a failed login into a 500 or lose the trace:
            # fall back to the server log, still answer like any failed login.
            log.exception("could not write login_failed audit entry: user=%r ip=%s reason=%s", body.username, ip, reason)
            await db.rollback()
        await asyncio.sleep(deadline - time.perf_counter())
        raise bad

    # This should be the case :D
    assert user is not None  # ruff: ignore[assert]
    throttle.record_success(body.username)

    user.last_login_at = now
    if (
        not user.must_change_password  # an OTP/admin-set password is about to be replaced anyway
        and user.auth_source == AuthSource.LOCAL
        and user.password_hash
        and password_needs_rehash(user.password_hash)
    ):
        user.password_hash = await hash_password_async(body.password)
    await _start_session(db, request, response, user)
    _audit_login(db, request, org.id, user, body.username, True)
    await db.commit()
    return _me(await principal_for_user(db, user, registry), user, org, registry)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_session)):
    """Deliberately does not require a valid session: if it already expired the
    user should still end up logged out (cookie cleared) instead of getting a 401."""
    settings = get_settings()
    check_origin(request)
    if cookie := request.cookies.get(settings.session_cookie_name):
        sess = (
            await db.execute(select(AuthSession).where(AuthSession.token_hash == hash_token(cookie)))
        ).scalar_one_or_none()
        if sess is not None:
            user = await db.get(User, sess.user_id)
            db.add(
                AuditLog(
                    org_id=sess.org_id,
                    actor_type="user",
                    actor_id=str(sess.user_id),
                    actor_name=user.username if user else "",
                    action="core.auth.logout",
                    ip=client_ip(request),
                    user_agent=(request.headers.get("user-agent") or "")[:400] or None,
                )
            )
            await db.delete(sess)
            await db.commit()
    response.delete_cookie(settings.session_cookie_name, path="/")


@router.get("/me", response_model=MeOut)
async def me(ctx: Ctx = Depends(get_ctx)):
    p = ctx.principal
    assert p is not None
    org = await ctx.db.get(Organization, ctx.org_id)
    user = await ctx.db.get(User, p.id) if p.kind == "user" else None
    return _me(p, user, org, ctx.registry)


@router.post("/password", status_code=204)
async def change_password(body: PasswordChangeIn, response: Response, ctx: Ctx = Depends(get_ctx)):
    p = ctx.principal
    if p is None or p.kind != "user":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only users have passwords")
    user = await ctx.get(User, p.id)
    if user.auth_source != AuthSource.LOCAL:
        raise AppError(status.HTTP_400_BAD_REQUEST, "auth.external_password", "password is managed by an external directory")
    # The same per-account counter as the login (smp/core/throttle.py), so an open
    # session (a teacher's unlocked PC) can't be used to guess the account's password.
    throttle: LoginThrottle = ctx.request.app.state.login_throttle
    ip = client_ip(ctx.request) or "unknown"
    if not throttle.allowed(ip, user.username):
        raise AppError(status.HTTP_429_TOO_MANY_REQUESTS, "auth.too_many_attempts", "too many attempts, try again later")
    throttle.record_attempt(ip)
    if not await verify_password_async(user.password_hash, body.current_password):
        throttle.record_failure(user.username)
        ctx.audit("core.auth.password_change_failed", user, {"reason": "wrong current password"})
        if throttle.locked(user.username):
            # Locked like after failed logins; and whoever is guessing loses the session.
            await ctx.db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
            ctx.audit("core.auth.sessions_ended", user, {"reason": "too many wrong current passwords"})
            await ctx.commit()
            raise AppError(status.HTTP_429_TOO_MANY_REQUESTS, "auth.too_many_attempts", "too many attempts, try again later")
        await ctx.commit()
        raise AppError(status.HTTP_400_BAD_REQUEST, "auth.wrong_current_password", "The current password is incorrect.")
    throttle.record_success(user.username)
    if body.new_password == body.current_password:
        raise AppError(
            status.HTTP_400_BAD_REQUEST, "auth.password_unchanged", "The new password must be different from the current one."
        )
    if problem := password_problem(body.new_password, user.username):
        raise AppError.from_problem(status.HTTP_400_BAD_REQUEST, problem)
    user.password_hash = await hash_password_async(body.new_password)
    was_forced = user.must_change_password
    user.must_change_password = False
    clear_otp(user)
    # Rotate the session: end every session of this user (including this one) and
    # start a fresh one, so a token known to anyone else stops working.
    await ctx.db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    await _start_session(ctx.db, ctx.request, response, user)
    ctx.audit("core.auth.password_change", user, {"forced": was_forced})
    await ctx.commit()


@router.patch("/preferences", response_model=PreferencesOut)
async def update_preferences(body: PreferencesPatch, ctx: Ctx = Depends(get_ctx)):
    """Save the signed-in user's own UI settings (not audited: they change nothing
    for anyone else). API tokens have no account to store them on."""
    p = ctx.principal
    if p is None or p.kind != "user":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only users have preferences")
    user = await ctx.get(User, p.id)
    current = _preferences(user)
    sizes = dict(current.page_size)
    for key, size in body.page_size.items():
        if size is None:
            sizes.pop(key, None)
        else:
            sizes[key] = size
    if len(sizes) > MAX_PAGE_SIZE_KEYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "too many saved page sizes")
    locale = body.locale if "locale" in body.model_fields_set else current.locale
    updated = PreferencesOut(page_size=sizes, locale=locale)
    # Assign a new dict: in-place changes to a JSON column are not detected.
    user.preferences = {**(user.preferences or {}), **updated.model_dump()}
    await ctx.commit()
    return updated


def _me(p, user: User | None, org, registry) -> MeOut:
    return MeOut(
        id=p.id,
        kind=p.kind,
        username=user.username if user else p.name,
        display_name=user.display_name if user else p.name,
        org_id=org.id,
        org_name=org.name,
        roles=[
            {
                "role": r.role,
                "room_id": str(r.room_id) if r.room_id else None,
                "building_id": str(r.building_id) if r.building_id else None,
            }
            for r in p.roles
        ],
        permissions=p.all_permissions(registry),
        room_permissions={str(k): sorted(v) for k, v in p.room_perms.items()},
        building_permissions={str(k): sorted(v) for k, v in p.building_perms.items()},
        room_buildings={str(k): str(v) for k, v in p.room_buildings.items()},
        must_change_password=bool(user and user.must_change_password),
        preferences=_preferences(user),
    )
