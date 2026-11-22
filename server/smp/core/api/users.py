import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, func, or_, select

from smp.core.api import ORMModel, Page
from smp.core.context import Ctx, get_ctx, require
from smp.core.errors import AppError
from smp.core.models import (
    AuthSession,
    AuthSource,
    Group,
    GroupMember,
    GroupTeacher,
    Role,
    RoleAssignment,
    Room,
    TargetType,
    User,
)
from smp.core.otp import clear_otp, issue_otps, otp_state, readable_otp
from smp.core.passwords import password_problem
from smp.core.policy import emit_policy_changed, remove_assignments_for
from smp.core.security import hash_password_async, require_secret_key
from smp.core.tokens import delete_tokens_of
from smp.core.xlsx_users import (
    DATETIME_FORMATS,
    FILE_NAMES,
    SAMPLE_ROWS,
    XLSX_MIME,
    Lang,
    natural_key,
    read_users,
    write_users,
)

router = APIRouter(prefix="/users", tags=["users"])


class RoleIn(BaseModel):
    role: Role
    room_id: uuid.UUID | None = None


class RoleOut(ORMModel):
    role: str
    room_id: uuid.UUID | None


class GroupRef(ORMModel):
    id: uuid.UUID
    name: str
    kind: str


class UserOut(ORMModel):
    id: uuid.UUID
    username: str
    display_name: str
    email: str | None
    auth_source: str
    external_id: str | None
    disabled: bool
    roles: list[RoleOut]
    groups: list[GroupRef]
    last_login_at: datetime | None
    created_at: datetime
    # Admin-only (null for everyone else): which accounts haven't been activated yet
    # is useful to whoever manages them and to nobody else.
    must_change_password: bool | None = None
    otp_status: str | None = None  # "active" | "expired" | null
    otp_expires_at: datetime | None = None
    # Whether the caller may issue a new one-time password for this user: admins for
    # every local user, class teachers for the students of groups they teach.
    can_reset_password: bool = False


class UserCreated(UserOut):
    one_time_password: str | None = None  # shown once; also in the credentials export until used


class PasswordResetOut(BaseModel):
    one_time_password: str | None = None  # set when an OTP was generated
    otp_expires_at: datetime | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._@-]+$")
    display_name: str = ""
    email: EmailStr | None = None
    # Omitted: a one-time password is generated (returned once, exportable until used).
    password: str | None = Field(default=None, max_length=1024)
    roles: list[RoleIn] = []
    group_ids: list[uuid.UUID] = []  # classes / groups to put the new user in


class UserUpdate(BaseModel):
    display_name: str | None = None
    email: EmailStr | None = None
    disabled: bool | None = None


class PasswordSet(BaseModel):
    password: str | None = Field(default=None, max_length=1024)  # omitted: generate a one-time password


def _check_admin_password(password: str, username: str) -> None:
    if problem := password_problem(password, username):
        raise AppError.from_problem(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)


RESET_CLASS = "core.users.reset_class_password"


async def _taught_students(ctx: Ctx) -> set[uuid.UUID]:
    """Members of the groups the caller teaches (empty unless they may reset)."""
    p = ctx.principal
    if p is None or p.kind != "user" or not p.can(RESET_CLASS):
        return set()
    taught = select(GroupTeacher.group_id).where(GroupTeacher.user_id == p.id)
    rows = await ctx.db.execute(
        select(GroupMember.user_id).where(GroupMember.org_id == ctx.org_id, GroupMember.group_id.in_(taught))
    )
    return set(rows.scalars())


def _may_reset(ctx: Ctx, user: User, taught: set[uuid.UUID]) -> bool:
    p = ctx.principal
    if p is None or user.auth_source != AuthSource.LOCAL:
        return False
    if p.can("core.users.write"):
        return True
    # Class teachers: only plain students of their groups. Never staff accounts (a
    # teacher must not be able to take over a colleague's or an admin's account).
    return user.id in taught and user.id != p.id and {r.role for r in user.roles} == {Role.STUDENT}


def _out(
    ctx: Ctx, user: User, taught: set[uuid.UUID], model: type[UserOut] = UserOut, **extra
) -> UserOut:
    out = model.model_validate(user).model_copy(update=extra)
    out.can_reset_password = _may_reset(ctx, user, taught)
    # Activation state only for those who manage the account (admins, its class
    # teachers); for everyone else it would just list accounts worth attacking.
    if out.can_reset_password:
        out.must_change_password = user.must_change_password
        out.otp_status = otp_state(user)
        out.otp_expires_at = user.otp_expires_at
    else:
        out.must_change_password = None
        out.otp_expires_at = None
    return out


async def _check_email_free(ctx: Ctx, email: str | None, except_user: uuid.UUID | None = None) -> None:
    """E-mail addresses are unique per school (case-insensitive); see the DB index."""
    if not email:
        return
    stmt = select(User.id).where(User.org_id == ctx.org_id, func.lower(User.email) == email.lower())
    if except_user is not None:
        stmt = stmt.where(User.id != except_user)
    if (await ctx.db.execute(stmt.limit(1))).first() is not None:
        raise AppError(status.HTTP_409_CONFLICT, "users.email_taken", "This e-mail address already belongs to another user.")


async def _validate_roles(ctx: Ctx, roles: list[RoleIn]) -> None:
    for r in roles:
        if r.room_id is not None:
            await ctx.get(Room, r.room_id)
            if r.role == Role.ADMIN:
                raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "users.admin_not_room_scoped", "admin role cannot be room-scoped")


def _event(u: User) -> dict:
    return {"id": str(u.id), "username": u.username}


def _filtered_users(ctx: Ctx, q: str | None, role: Role | None, group_id: uuid.UUID | None):
    """The user list query with the UI's filters. Shared by the list and the export so
    "export what I see" holds by construction."""
    stmt = ctx.select(User)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.display_name.ilike(like), User.email.ilike(like)))
    if role:
        stmt = stmt.where(User.roles.any(RoleAssignment.role == role))
    if group_id:
        stmt = stmt.where(User.id.in_(select(GroupMember.user_id).where(GroupMember.group_id == group_id)))
    return stmt


@router.get("", response_model=Page[UserOut])
async def list_users(
    ctx: Ctx = Depends(require("core.users.read")),
    q: str | None = None,
    role: Role | None = None,
    group_id: uuid.UUID | None = None,
    limit: int = Query(100, le=1000),
    offset: int = 0,
):
    stmt = _filtered_users(ctx, q, role, group_id)
    total = await ctx.db.scalar(stmt.with_only_columns(func.count()).order_by(None))
    rows = await ctx.db.execute(stmt.order_by(User.username).limit(limit).offset(offset))
    taught = await _taught_students(ctx)
    return Page(items=[_out(ctx, u, taught) for u in rows.scalars()], total=total or 0)


@router.post("", response_model=UserCreated, status_code=201)
async def create_user(body: UserCreate, ctx: Ctx = Depends(require("core.users.write"))):
    """Every new local user can log in: with the given password, or with a generated
    one-time password. Either way they must choose their own at first login."""
    await _validate_roles(ctx, body.roles)
    await _check_email_free(ctx, body.email)
    if body.group_ids:
        ctx.check("core.groups.write")
    if body.password is not None:
        _check_admin_password(body.password, body.username)
    user = User(
        org_id=ctx.org_id,
        username=body.username,
        display_name=body.display_name or body.username,
        email=body.email,
        auth_source=AuthSource.LOCAL,
        must_change_password=True,
        roles=[RoleAssignment(org_id=ctx.org_id, role=r.role, room_id=r.room_id) for r in body.roles],
    )
    otp = None
    if body.password is not None:
        user.password_hash = await hash_password_async(body.password)
    else:
        (otp,) = await issue_otps(ctx, [user])
    ctx.db.add(user)
    await ctx.db.flush()
    await _set_groups(ctx, user, set(body.group_ids))
    ctx.audit(
        "core.user.create",
        user,
        {
            "username": user.username,
            "roles": [r.model_dump(mode="json") for r in body.roles],
            "group_ids": sorted(map(str, body.group_ids)),
            "one_time_password": otp is not None,  # never the value itself
        },
    )
    ctx.emit("core.user.created", _event(user))
    await ctx.commit()
    await ctx.db.refresh(user, ["groups"])
    return _out(ctx, user, set(), UserCreated, one_time_password=otp)


async def _set_groups(ctx: Ctx, user: User, wanted: set[uuid.UUID]) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Make `user` a member of exactly `wanted`. Returns (added, removed) group ids
    and emits the same events as editing the groups directly."""
    if wanted:
        rows = await ctx.db.execute(select(Group.id).where(Group.org_id == ctx.org_id, Group.id.in_(wanted)))
        if missing := wanted - set(rows.scalars()):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown groups: {sorted(map(str, missing))}")
    current = set(
        (await ctx.db.execute(select(GroupMember.group_id).where(GroupMember.user_id == user.id))).scalars()
    )
    added, removed = wanted - current, current - wanted
    if removed:
        await ctx.db.execute(
            delete(GroupMember).where(GroupMember.user_id == user.id, GroupMember.group_id.in_(removed))
        )
    ctx.db.add_all(GroupMember(org_id=ctx.org_id, group_id=g, user_id=user.id) for g in added)
    for g in added | removed:
        ctx.emit("core.group.members_changed", {"id": str(g)})
    if added or removed:
        emit_policy_changed(ctx, user_id=user.id)
    return added, removed


# --------------------------------------------------------------------------- xlsx export / import
# All declared before /{user_id} so e.g. "export.xlsx" isn't parsed as a user id.

MAX_IMPORT_BYTES = 5 * 1024 * 1024


async def _read_limited(request: Request, limit: int) -> bytes:
    """Read the body but stop as soon as it exceeds `limit`, also for chunked uploads
    that have no Content-Length (otherwise the whole stream is buffered first)."""
    if int(request.headers.get("content-length") or 0) > limit:
        raise AppError(status.HTTP_413_CONTENT_TOO_LARGE, "upload.too_large", "file too large (max 5 MB)", max_mb=5)
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise AppError(status.HTTP_413_CONTENT_TOO_LARGE, "upload.too_large", "file too large (max 5 MB)", max_mb=5)
    return bytes(buf)


def _xlsx(data: bytes, filename: str) -> Response:
    return Response(data, media_type=XLSX_MIME, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export.xlsx", response_class=Response, responses={200: {"content": {XLSX_MIME: {}}}})
async def export_users(
    ctx: Ctx = Depends(require("core.users.read")),
    q: str | None = None,
    role: Role | None = None,
    group_id: uuid.UUID | None = None,
    lang: Lang = "en",
):
    """The users matching the same filters as GET /users, as Name | Username | Class
    (the user's class-type groups, comma separated). `lang` picks the column headers
    (en: Name | Username | Class, hu: Név | Felhasználónév | Osztály)."""
    stmt = _filtered_users(ctx, q, role, group_id).order_by(User.display_name, User.username).limit(20000)
    users = list((await ctx.db.execute(stmt)).scalars())
    rows = [
        (u.display_name, u.username, ", ".join(sorted((g.name for g in u.groups if g.kind == "class"), key=natural_key)))
        for u in users
    ]
    # Exporting personal data is worth an audit entry (GDPR: who took a copy of what).
    ctx.audit(
        "core.user.export",
        target_type="user",
        details={"count": len(rows), "filter": {"q": q, "role": role, "group_id": str(group_id) if group_id else None}},
    )
    await ctx.commit()
    return _xlsx(write_users(rows, lang), f"{FILE_NAMES[lang][0]}-{date.today().isoformat()}.xlsx")


@router.get("/credentials.xlsx", response_class=Response, responses={200: {"content": {XLSX_MIME: {}}}})
async def export_credentials(
    ctx: Ctx = Depends(get_ctx),
    q: str | None = None,
    role: Role | None = None,
    group_id: uuid.UUID | None = None,
    lang: Lang = "en",
):
    """Login slips: the filtered users that have a usable (unused, unexpired) one-time
    password, with that password. Admins get every such user; class teachers only the
    students they may reset (members of groups they teach). Audited. The first three
    columns are the import format, so the file also works as a user list."""
    p = ctx.principal
    assert p is not None
    is_admin = p.can("core.users.write")
    if not is_admin and not p.can(RESET_CLASS):
        raise AppError(
            status.HTTP_403_FORBIDDEN, "permission.missing", "missing permission core.users.write", permission="core.users.write"
        )
    require_secret_key()
    taught = set() if is_admin else await _taught_students(ctx)
    org = await ctx.get_org()
    tz = ZoneInfo(org.timezone)
    stmt = _filtered_users(ctx, q, role, group_id).where(User.otp_secret.is_not(None))
    users = list((await ctx.db.execute(stmt.order_by(User.display_name, User.username).limit(20000))).scalars())
    rows = []
    for u in users:
        if not is_admin and not _may_reset(ctx, u, taught):
            continue  # class teachers: only their own students
        otp = readable_otp(u)
        if otp is None:  # expired, or SMP_SECRET_KEY changed since it was issued
            continue
        classes = ", ".join(sorted((g.name for g in u.groups if g.kind == "class"), key=natural_key))
        until = u.otp_expires_at.astimezone(tz).strftime(DATETIME_FORMATS[lang]) if u.otp_expires_at else ""
        rows.append((u.display_name, u.username, classes, otp, until))
    ctx.audit(
        "core.user.credentials_export",
        target_type="user",
        details={
            "count": len(rows),
            "as": "admin" if is_admin else "class_teacher",
            "usernames": [r[1] for r in rows],
            "filter": {"q": q, "role": role, "group_id": str(group_id) if group_id else None},
        },
    )
    await ctx.commit()
    return _xlsx(write_users(rows, lang, credentials=True), f"{FILE_NAMES[lang][1]}-{date.today().isoformat()}.xlsx")


@router.get("/import-sample.xlsx", response_class=Response, responses={200: {"content": {XLSX_MIME: {}}}})
async def import_sample(lang: Lang = "en", ctx: Ctx = Depends(require("core.users.read"))):
    """Example file in the import/export format, with `lang` headers (same content as
    docs/samples/users-sample.xlsx and felhasznalok-minta.xlsx)."""
    return _xlsx(write_users(SAMPLE_ROWS, lang), FILE_NAMES[lang][2])


class ImportIssue(BaseModel):
    row: int  # spreadsheet row; 0 = the file itself
    error: str  # English message
    code: str  # stable code the UI translates (see smp/core/errors.py)
    params: dict[str, str | int] = {}


class ImportResult(BaseModel):
    dry_run: bool
    created: list[str] = []  # usernames
    skipped: list[str] = []  # usernames that already exist (left untouched)
    groups_created: list[str] = []
    errors: list[ImportIssue] = []


@router.post(
    "/import",
    response_model=ImportResult,
    openapi_extra={"requestBody": {"required": True, "content": {XLSX_MIME: {"schema": {"type": "string", "format": "binary"}}}}},
)
async def import_users(
    request: Request,
    dry_run: bool = False,
    ctx: Ctx = Depends(require("core.users.write", "core.groups.write")),
):
    """Create students from an .xlsx (body = the raw file). Each gets their own
    one-time password (download them with GET /users/credentials.xlsx) and must choose
    a password at first login; usernames that already exist are skipped. Any invalid
    row rejects the whole file (422, detail = ImportResult). With dry_run=true nothing
    is written: use it to preview."""
    require_secret_key()  # fail before the preview, not after
    data = await _read_limited(request, MAX_IMPORT_BYTES)

    parsed = read_users(data)
    if parsed.errors:
        result = ImportResult(dry_run=dry_run, errors=[ImportIssue(row=r, error=p.message, code=p.code, params=p.params) for r, p in parsed.errors])
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, result.model_dump())

    usernames = [r.username for r in parsed.rows]
    existing = set(
        (await ctx.db.execute(select(User.username).where(User.org_id == ctx.org_id, User.username.in_(usernames)))).scalars()
    ) if usernames else set()
    new_rows = [r for r in parsed.rows if r.username not in existing]

    wanted_groups = {c for r in new_rows for c in r.classes}
    groups: dict[str, Group] = {}
    if wanted_groups:
        rows = await ctx.db.execute(select(Group).where(Group.org_id == ctx.org_id, Group.name.in_(wanted_groups)))
        groups = {g.name: g for g in rows.scalars()}
    missing_groups = sorted(wanted_groups - set(groups), key=natural_key)

    result = ImportResult(
        dry_run=dry_run,
        created=[r.username for r in new_rows],
        skipped=[r.username for r in parsed.rows if r.username in existing],
        groups_created=missing_groups,
    )
    if dry_run or not new_rows:
        return result

    for name in missing_groups:
        g = Group(org_id=ctx.org_id, name=name, kind="class", description="Created by import")
        ctx.db.add(g)
        groups[name] = g
    users = [
        User(
            org_id=ctx.org_id,
            username=r.username,
            display_name=r.name,
            auth_source=AuthSource.LOCAL,
            roles=[RoleAssignment(org_id=ctx.org_id, role=Role.STUDENT)],
        )
        for r in new_rows
    ]
    await issue_otps(ctx, users)  # an individual one-time password each
    ctx.db.add_all(users)
    await ctx.db.flush()
    touched_groups: set[uuid.UUID] = set()
    for r, u in zip(new_rows, users, strict=True):
        for c in r.classes:
            ctx.db.add(GroupMember(org_id=ctx.org_id, group_id=groups[c].id, user_id=u.id))
            touched_groups.add(groups[c].id)
        ctx.emit("core.user.created", _event(u))
    for g in missing_groups:
        ctx.emit("core.group.created", {"id": str(groups[g].id), "name": g})
    for gid in touched_groups:
        ctx.emit("core.group.members_changed", {"id": str(gid)})
    if touched_groups:
        emit_policy_changed(ctx, reason="user import")
    ctx.audit(
        "core.user.import",
        target_type="user",
        details={
            "created": result.created,
            "skipped": result.skipped,
            "groups_created": missing_groups,
        },
    )
    await ctx.commit()
    return result


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, ctx: Ctx = Depends(require("core.users.read"))):
    return _out(ctx, await ctx.get(User, user_id), await _taught_students(ctx))


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: uuid.UUID, body: UserUpdate, ctx: Ctx = Depends(require("core.users.write"))):
    user = await ctx.get(User, user_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("disabled") and user.id == ctx.principal.id:
        raise AppError(status.HTTP_400_BAD_REQUEST, "users.cannot_disable_self", "you cannot disable yourself")
    if "email" in changes:
        await _check_email_free(ctx, changes["email"], except_user=user.id)
    for k, v in changes.items():
        setattr(user, k, v)
    if changes.get("disabled"):
        await ctx.db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    ctx.audit("core.user.update", user, changes)
    ctx.emit("core.user.updated", _event(user))
    await ctx.commit()
    return _out(ctx, user, set())


@router.put("/{user_id}/roles", response_model=UserOut)
async def set_roles(user_id: uuid.UUID, body: list[RoleIn], ctx: Ctx = Depends(require("core.users.write"))):
    user = await ctx.get(User, user_id)
    await _validate_roles(ctx, body)
    if user.id == ctx.principal.id and not any(r.role == Role.ADMIN for r in body):
        if any(ra.role == Role.ADMIN for ra in user.roles):
            raise AppError(status.HTTP_400_BAD_REQUEST, "users.cannot_remove_own_admin", "you cannot remove your own admin role")
    unique = {(r.role, r.room_id) for r in body}
    # Delete first: the unit of work would otherwise insert before deleting and trip
    # the (user, role, room) unique constraint.
    await ctx.db.execute(delete(RoleAssignment).where(RoleAssignment.user_id == user.id))
    ctx.db.expire(user, ["roles"])
    ctx.db.add_all(RoleAssignment(org_id=ctx.org_id, user_id=user.id, role=role, room_id=room) for role, room in unique)
    ctx.audit("core.user.roles", user, {"roles": [{"role": r, "room_id": str(rm) if rm else None} for r, rm in unique]})
    ctx.emit("core.user.updated", _event(user))
    await ctx.commit()
    await ctx.db.refresh(user, ["roles"])
    return _out(ctx, user, set())


@router.put("/{user_id}/groups", response_model=UserOut)
async def set_groups(
    user_id: uuid.UUID, group_ids: list[uuid.UUID], ctx: Ctx = Depends(require("core.groups.write"))
):
    """Replace the user's class/group memberships (the user-side view of
    PUT /groups/{id}/members)."""
    user = await ctx.get(User, user_id)
    added, removed = await _set_groups(ctx, user, set(group_ids))
    ctx.audit(
        "core.user.groups",
        user,
        {"added": sorted(map(str, added)), "removed": sorted(map(str, removed))},
    )
    await ctx.commit()
    await ctx.db.refresh(user, ["groups"])
    return _out(ctx, user, set())


@router.post("/{user_id}/password", response_model=PasswordResetOut)
async def set_password(user_id: uuid.UUID, body: PasswordSet, ctx: Ctx = Depends(get_ctx)):
    """Reset: a given password (admins only), or (body without password) a new
    one-time password (admins, or class teachers for their students). Either way all
    sessions end and the user must choose a new password."""
    p = ctx.principal
    assert p is not None
    is_admin = p.can("core.users.write")
    # Permission first, so callers without it can't probe which user ids exist.
    if not is_admin and not p.can(RESET_CLASS):
        raise AppError(
            status.HTTP_403_FORBIDDEN, "permission.missing", "missing permission core.users.write", permission="core.users.write"
        )
    if not is_admin and body.password is not None:
        raise AppError(
            status.HTTP_403_FORBIDDEN, "users.typed_password_admin_only", "only administrators can set a typed password"
        )
    user = await ctx.get(User, user_id)
    if user.auth_source != AuthSource.LOCAL:
        raise AppError(status.HTTP_400_BAD_REQUEST, "auth.external_password", "password is managed by an external directory")
    if not is_admin and not _may_reset(ctx, user, await _taught_students(ctx)):
        raise AppError(
            status.HTTP_403_FORBIDDEN, "users.reset_own_students_only", "you can only reset students of groups you teach"
        )
    otp = None
    if body.password is not None:
        _check_admin_password(body.password, user.username)
        user.password_hash = await hash_password_async(body.password)
        user.must_change_password = True
        clear_otp(user)
    else:
        (otp,) = await issue_otps(ctx, [user])
    await ctx.db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    ctx.audit(
        "core.user.password_reset",
        user,
        {"one_time_password": otp is not None, "as": "admin" if is_admin else "class_teacher"},
    )
    await ctx.commit()
    return PasswordResetOut(one_time_password=otp, otp_expires_at=user.otp_expires_at)


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: uuid.UUID, ctx: Ctx = Depends(require("core.users.write"))):
    user = await ctx.get(User, user_id)
    if user.id == ctx.principal.id:
        raise AppError(status.HTTP_400_BAD_REQUEST, "users.cannot_delete_self", "you cannot delete yourself")
    removed = await remove_assignments_for(ctx, TargetType.USER, user.id)
    # Their API tokens go with them (otherwise a deleted admin's token keeps working).
    if tokens := await delete_tokens_of(ctx, user.id):
        ctx.audit(
            "core.token.delete_with_user",
            user,
            {"username": user.username, "count": len(tokens), "tokens": tokens},
        )
    ctx.audit("core.user.delete", user, {"username": user.username, "removed_assignments": removed})
    ctx.emit("core.user.deleted", _event(user))
    if removed:
        emit_policy_changed(ctx, user_id=user.id)
    await ctx.db.delete(user)
    await ctx.commit()
