# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, EmailStr, Field, model_validator
from sqlalchemy import ColumnElement, delete, false, func, or_, select

from smp.core import roles as role_table
from smp.core.api import ORMModel, Page
from smp.core.context import Ctx, Principal, get_ctx, require
from smp.core.errors import AppError
from smp.core.models import (
    AuthSession,
    AuthSource,
    Building,
    Group,
    GroupBuilding,
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
from smp.core.uploads import read_limited
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
    """One role assignment: org-wide (no scope), for a building or for a room (never
    both). Where each role may sit is the table in smp/core/roles.py."""

    role: Role
    room_id: uuid.UUID | None = None
    building_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_scope(self) -> RoleIn:
        if self.room_id is not None and self.building_id is not None:
            raise ValueError("room_id and building_id are mutually exclusive")
        return self


class RoleOut(ORMModel):
    role: str
    room_id: uuid.UUID | None
    building_id: uuid.UUID | None = None


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
    # Whether the caller may issue a new one-time password for this user: whoever may
    # edit them (for every local user), class teachers for the students of groups they teach.
    can_reset_password: bool = False
    # Whether the caller may edit this user (update, delete, roles, groups).
    can_edit: bool = False


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
USERS_READ = "core.users.read"
USERS_WRITE = "core.users.write"
GROUPS_READ = "core.groups.read"
GROUPS_WRITE = "core.groups.write"
TOKENS_MANAGE = "core.tokens.manage"

Assignment = tuple[str, uuid.UUID | None, uuid.UUID | None]  # (role, room_id, building_id)


def users_in_buildings(buildings: set[uuid.UUID]) -> ColumnElement[bool]:
    """SQL condition: the user has an assignment in one of these buildings, directly or
    through a room of one. This is how a user "belongs" to a building."""
    if not buildings:
        return false()
    rooms = select(Room.id).where(Room.building_id.in_(buildings))
    return User.roles.any(or_(RoleAssignment.building_id.in_(buildings), RoleAssignment.room_id.in_(rooms)))


def visible_user_buildings(p: Principal) -> set[uuid.UUID] | None:
    """The buildings whose users the caller may see (None = every user): the union of
    their users.read and users.write buildings, unless one of them is school-wide."""
    read_b, write_b = p.buildings_with(USERS_READ), p.buildings_with(USERS_WRITE)
    if read_b is None or write_b is None:
        return None
    return read_b | write_b


def group_visible(p: Principal, g: Group) -> bool:
    """May the caller see this group? Everything for a school-wide reader; otherwise a
    group with a building among theirs (a school-wide group has none)."""
    read_b, write_b = p.buildings_with(GROUPS_READ), p.buildings_with(GROUPS_WRITE)
    if read_b is None or write_b is None:
        return True
    return bool({b.id for b in g.buildings} & (read_b | write_b))


def _token_managing_roles(ctx: Ctx) -> set[str]:
    """The roles whose holders may create API tokens (from the permission defaults, so
    no role is named here)."""
    out = set()
    for name in role_table.ROLE_DEFS:
        perms = ctx.registry.role_permissions(name)
        if "*" in perms or TOKENS_MANAGE in perms:
            out.add(name)
    return out


def _grantable_roles(ctx: Ctx, p: Principal) -> frozenset[str]:
    """The roles the caller may give to others (see roles.can_grant). A token acts for
    nobody in particular: the wildcard token for everyone, any other one for every role
    except those that can manage tokens (it must not mint staff above itself)."""
    if p.kind == "user":
        return frozenset(r for r in role_table.ROLE_DEFS if any(role_table.can_grant(sr.role, r) for sr in p.roles))
    if "*" in p.global_perms:
        return frozenset(role_table.ROLE_DEFS)
    return frozenset(set(role_table.ROLE_DEFS) - _token_managing_roles(ctx))


@dataclass
class _Access:
    """What the caller may do with which users, worked out once per request.

    read_b / write_b are the buildings of core.users.read / core.users.write (None = the
    whole school). A user belongs to the buildings of their assignments (a building
    scope, or the building of a room scope); an org-wide assignment, a room in no
    building, or no assignment at all means "outside every building".
    """

    ctx: Ctx
    p: Principal
    taught: set[uuid.UUID]
    read_b: set[uuid.UUID] | None
    write_b: set[uuid.UUID] | None
    grantable: frozenset[str]
    room_b: dict[uuid.UUID, uuid.UUID | None] = field(default_factory=dict)

    @property
    def has_write(self) -> bool:
        return self.write_b is None or bool(self.write_b)

    @property
    def org_wide_write(self) -> bool:
        return self.write_b is None

    async def load_rooms(self, room_ids: Iterable[uuid.UUID]) -> None:
        missing = {r for r in room_ids if r not in self.room_b}
        if not missing:
            return
        rows = await self.ctx.db.execute(select(Room.id, Room.building_id).where(Room.id.in_(missing)))
        self.room_b.update({room_id: building_id for room_id, building_id in rows})
        self.room_b.update({r: None for r in missing if r not in self.room_b})

    async def load_users(self, users: Iterable[User]) -> None:
        await self.load_rooms({ra.room_id for u in users for ra in u.roles if ra.room_id is not None})

    def scope(self, assignments: Iterable[tuple[uuid.UUID | None, uuid.UUID | None]]) -> tuple[set[uuid.UUID], bool]:
        """(buildings, outside) of (room_id, building_id) pairs; `outside` is True when
        something is not inside a building, or there is nothing at all."""
        buildings: set[uuid.UUID] = set()
        outside = False
        found = False
        for room_id, building_id in assignments:
            found = True
            b = building_id if building_id is not None else self.room_b.get(room_id) if room_id is not None else None
            if b is None:
                outside = True
            else:
                buildings.add(b)
        return buildings, outside or not found

    def user_scope(self, user: User) -> tuple[set[uuid.UUID], bool]:
        return self.scope((ra.room_id, ra.building_id) for ra in user.roles)

    def user_buildings(self, user: User) -> set[uuid.UUID]:
        return self.user_scope(user)[0]

    def visible(self, user: User) -> bool:
        vis = visible_user_buildings(self.p)
        if vis is None or (self.p.kind == "user" and user.id == self.p.id):
            return True
        return bool(self.user_buildings(user) & vis)

    def may_edit(self, user: User) -> bool:
        """Edit = update, delete, roles, groups, password/OTP reset, credentials export.
        Every role the target holds must be one the caller may grant, and, for a caller
        limited to buildings, every assignment must lie inside them (so a user with an
        assignment elsewhere, or none, is out of reach)."""
        if not self.has_write or not {ra.role for ra in user.roles} <= self.grantable:
            return False
        if self.write_b is None:
            return True
        buildings, outside = self.user_scope(user)
        return not outside and buildings <= self.write_b

    def may_reset(self, user: User) -> bool:
        if user.auth_source != AuthSource.LOCAL:
            return False
        if self.may_edit(user):
            return True
        # Class teachers: only plain students of their groups. Never staff accounts (a
        # teacher must not be able to take over a colleague's or an admin's account).
        return user.id in self.taught and user.id != self.p.id and {r.role for r in user.roles} == {Role.STUDENT}


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


async def _access(ctx: Ctx, users: Iterable[User] = ()) -> _Access:
    p = ctx.principal
    assert p is not None
    acc = _Access(
        ctx,
        p,
        await _taught_students(ctx),
        p.buildings_with(USERS_READ),
        p.buildings_with(USERS_WRITE),
        _grantable_roles(ctx, p),
    )
    await acc.load_users(users)
    return acc


def _building_ids(*sets: set[uuid.UUID]) -> list[uuid.UUID] | None:
    """The audit building_ids for the union of these sets (None: an org-level entry)."""
    union = set().union(*sets)
    return sorted(union) if union else None


def _not_found(model: str) -> AppError:
    return AppError(status.HTTP_404_NOT_FOUND, "not_found", f"{model} not found", entity=model)


async def _load_user(ctx: Ctx, user_id: uuid.UUID, acc: _Access, *, edit: bool = False) -> User:
    """The user, or 404 when the caller can't see them (a manager never learns that a
    user of another building exists), or 403 when they may see but not edit them."""
    user = await ctx.get(User, user_id)
    await acc.load_users([user])
    if not acc.visible(user):
        raise _not_found("User")
    if edit and not acc.may_edit(user):
        raise AppError(
            status.HTTP_403_FORBIDDEN, "users.not_editable", "you may not change this user (outside your buildings or above your level)"
        )
    return user


def _out(acc: _Access, user: User, model: type[UserOut] = UserOut, **extra) -> UserOut:
    out = model.model_validate(user).model_copy(update=extra)
    # A caller limited to buildings sees only the groups of their buildings.
    out.groups = [gr for gr, g in zip(out.groups, user.groups, strict=True) if group_visible(acc.p, g)]
    out.can_edit = acc.may_edit(user)
    out.can_reset_password = acc.may_reset(user)
    # Activation state only for those who manage the account (editors, its class
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


async def _grant(ctx: Ctx, acc: _Access, roles: list[RoleIn]) -> list[Assignment]:
    """Check the requested assignments and turn them into rows. A caller limited to
    buildings (a manager) gives a role without a scope to every one of their
    buildings; everything else must be in their buildings or the rooms of them, and
    only roles they may grant (roles.can_grant) at scopes the role allows
    (roles.allowed_scope)."""
    out: dict[Assignment, None] = {}
    for r in roles:
        role = r.role.value
        kind = role_table.SCOPE_ROOM if r.room_id else role_table.SCOPE_BUILDING if r.building_id else role_table.SCOPE_ORG
        if kind == role_table.SCOPE_ROOM and role == Role.ADMIN:
            raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "users.admin_not_room_scoped", "admin role cannot be room-scoped")
        if role not in acc.grantable:
            raise AppError(
                status.HTTP_403_FORBIDDEN, "users.role_not_grantable", f"you may not grant the {role} role", role=role
            )
        if kind == role_table.SCOPE_ORG and acc.write_b is not None and role_table.allowed_scope(role, role_table.SCOPE_BUILDING):
            out.update({(role, None, b): None for b in sorted(acc.write_b)})
            continue
        if not role_table.allowed_scope(role, kind):
            raise AppError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "users.scope_not_allowed",
                f"the {role} role cannot be assigned at {kind} scope",
                role=role,
                scope=kind,
            )
        out_of_scope = AppError(
            status.HTTP_403_FORBIDDEN, "users.out_of_scope", "that building or room is outside the buildings you manage"
        )
        if r.building_id is not None:
            if acc.write_b is not None and r.building_id not in acc.write_b:
                raise out_of_scope
            await ctx.get(Building, r.building_id)
        elif r.room_id is not None:
            room = await ctx.get(Room, r.room_id)
            acc.room_b[room.id] = room.building_id
            if acc.write_b is not None and room.building_id not in acc.write_b:
                raise out_of_scope
        out[(role, r.room_id, r.building_id)] = None
    return list(out)


def _org_scoped(roles: Iterable[tuple[str, uuid.UUID | None, uuid.UUID | None]]) -> set[str]:
    return {role for role, room_id, building_id in roles if room_id is None and building_id is None}


def _org_perms_shrink(
    ctx: Ctx,
    before: Iterable[tuple[str, uuid.UUID | None, uuid.UUID | None]],
    after: Iterable[tuple[str, uuid.UUID | None, uuid.UUID | None]],
) -> bool:
    """Do the user's org-wide permissions lose anything? Their tokens may hold any of
    them (a token can't hold more than its creator had), so e.g. an admin made a unit
    manager must not keep a token with core.settings.write."""

    def perms(roles) -> set[str]:
        out: set[str] = set()
        for role in _org_scoped(roles):
            out |= ctx.registry.role_permissions(role)
        return out

    old, new = perms(before), perms(after)
    if "*" in new:
        return False
    return "*" in old or not old <= new


async def _drop_tokens(ctx: Ctx, user: User, action: str) -> None:
    """Delete the API tokens `user` created (and their child tokens) and audit which
    ones, without anything that could authenticate. Tokens are created by roles with
    core.tokens.manage, so this runs when such a user is deleted or loses any of their
    org-wide permissions (a role removed or lowered, e.g. admin -> unit manager).
    Otherwise their tokens would keep rights the user no longer has."""
    if tokens := await delete_tokens_of(ctx, user.id):
        ctx.audit(action, user, {"username": user.username, "count": len(tokens), "tokens": tokens})


def _event(u: User) -> dict:
    return {"id": str(u.id), "username": u.username}


def _filtered_users(
    ctx: Ctx, q: str | None, role: Role | None, group_id: uuid.UUID | None, within: set[uuid.UUID] | None = None
):
    """The user list query with the UI's filters. Shared by the list and the export so
    "export what I see" holds by construction. `within`: only users of these buildings
    (None = no restriction)."""
    stmt = ctx.select(User)
    if within is not None:
        stmt = stmt.where(users_in_buildings(within))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.display_name.ilike(like), User.email.ilike(like)))
    if role:
        stmt = stmt.where(User.roles.any(RoleAssignment.role == role))
    if group_id:
        stmt = stmt.where(User.id.in_(select(GroupMember.user_id).where(GroupMember.group_id == group_id)))
    return stmt


@router.get("", response_model=Page[UserOut])
async def list_users(  # ruff: ignore[missing-return-type-public-function, too-many-arguments, too-many-positional-arguments]
    ctx: Annotated[Ctx, Depends(require(USERS_READ, any_room=True))],
    q: str | None = None,
    role: Role | None = None,
    group_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(le=1000)] = 100,
    offset: int = 0,
):
    """Everyone for an org-wide reader; otherwise the users with an assignment in the caller's buildings."""  # ruff: ignore[docstring-missing-returns, line-too-long]
    acc = await _access(ctx)
    stmt = _filtered_users(ctx, q, role, group_id, acc.read_b)
    total = await ctx.db.scalar(stmt.with_only_columns(func.count()).order_by(None))  # ruff: ignore[line-too-long]
    rows = await ctx.db.execute(stmt.order_by(User.username).limit(limit).offset(offset))  # ruff: ignore[line-too-long]
    users = list(rows.scalars())
    await acc.load_users(users)
    return Page(items=[_out(acc, u) for u in users], total=total or 0)


@router.post("", response_model=UserCreated, status_code=201)
async def create_user(body: UserCreate, ctx: Ctx = Depends(require(USERS_WRITE, any_room=True))):
    """Every new local user can log in: with the given password, or with a generated
    one-time password. Either way they must choose their own at first login.

    A manager (limited to buildings) may only create teachers and students; a role
    without a scope goes to every building they manage."""
    acc = await _access(ctx)
    if not body.roles and not acc.org_wide_write:
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "users.role_required", "give the new user at least one role (it decides their building)"
        )
    assignments = await _grant(ctx, acc, body.roles)
    await _check_email_free(ctx, body.email)
    if body.group_ids:
        _check_groups_anywhere(ctx)
    if body.password is not None:
        _check_admin_password(body.password, body.username)
    user = User(
        org_id=ctx.org_id,
        username=body.username,
        display_name=body.display_name or body.username,
        email=body.email,
        auth_source=AuthSource.LOCAL,
        must_change_password=True,
        roles=[RoleAssignment(org_id=ctx.org_id, role=role, room_id=room, building_id=b) for role, room, b in assignments],
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
            "roles": [{"role": r, "room_id": str(rm) if rm else None, "building_id": str(b) if b else None} for r, rm, b in assignments],
            "group_ids": sorted(map(str, body.group_ids)),
            "one_time_password": otp is not None,  # never the value itself
        },
        building_ids=_building_ids(acc.user_buildings(user)),
    )
    ctx.emit("core.user.created", _event(user))
    await ctx.commit()
    await ctx.db.refresh(user, ["groups"])
    return _out(acc, user, UserCreated, one_time_password=otp)


def _check_groups_anywhere(ctx: Ctx) -> None:
    p = ctx.principal
    if p is None or not p.can_anywhere(GROUPS_WRITE):
        ctx.check(GROUPS_WRITE)  # raises permission.missing


async def _set_groups(ctx: Ctx, user: User, wanted: set[uuid.UUID]) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Make `user` a member of exactly `wanted`. Returns (added, removed) group ids
    and emits the same events as editing the groups directly.

    A caller limited to buildings (core.groups.write) may only join the user to groups
    inside their buildings, and leaves the user's other memberships alone."""
    assert ctx.principal is not None
    write_b = ctx.principal.buildings_with(GROUPS_WRITE)

    def editable(g: Group) -> bool:
        return write_b is None or (bool(g.buildings) and {b.id for b in g.buildings} <= write_b)

    groups: dict[uuid.UUID, Group] = {}
    current = set(
        (await ctx.db.execute(select(GroupMember.group_id).where(GroupMember.user_id == user.id))).scalars()
    )
    if wanted or current:
        rows = await ctx.db.execute(select(Group).where(Group.org_id == ctx.org_id, Group.id.in_(wanted | current)))
        groups = {g.id: g for g in rows.scalars()}
    if missing := wanted - set(groups):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown groups: {sorted(map(str, missing))}")
    added = wanted - current
    if write_b is not None:
        if any(not editable(groups[g]) for g in added):
            raise AppError(
                status.HTTP_403_FORBIDDEN, "groups.out_of_scope", "that group is outside the buildings you manage"
            )
        removed = {g for g in current - wanted if editable(groups[g])}  # others are not theirs to touch
    else:
        removed = current - wanted
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


def _xlsx(data: bytes, filename: str) -> Response:
    return Response(data, media_type=XLSX_MIME, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export.xlsx", response_class=Response, responses={200: {"content": {XLSX_MIME: {}}}})
async def export_users(
    ctx: Ctx = Depends(require(USERS_READ, any_room=True)),
    q: str | None = None,
    role: Role | None = None,
    group_id: uuid.UUID | None = None,
    lang: Lang = "en",
):
    """The users matching the same filters as GET /users, as Name | Username | Class
    (the user's class-type groups, comma separated). `lang` picks the column headers
    (en: Name | Username | Class, hu: Név | Felhasználónév | Osztály)."""
    acc = await _access(ctx)
    stmt = _filtered_users(ctx, q, role, group_id, acc.read_b).order_by(User.display_name, User.username).limit(20000)
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
        building_ids=_building_ids(acc.read_b or set()),
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
    password, with that password. Whoever may edit users gets every such user they may
    edit (a manager: those of their buildings); class teachers only the students they
    may reset (members of groups they teach). Audited. The first three columns are the
    import format, so the file also works as a user list."""
    acc = await _access(ctx)
    is_editor = acc.has_write
    if not is_editor and not ctx.principal.can(RESET_CLASS):  # type: ignore[union-attr]
        raise AppError(
            status.HTTP_403_FORBIDDEN, "permission.missing", "missing permission core.users.write", permission="core.users.write"
        )
    require_secret_key()
    org = await ctx.get_org()
    tz = ZoneInfo(org.timezone)
    stmt = _filtered_users(ctx, q, role, group_id).where(User.otp_secret.is_not(None))
    users = list((await ctx.db.execute(stmt.order_by(User.display_name, User.username).limit(20000))).scalars())
    await acc.load_users(users)
    rows = []
    for u in users:
        if not acc.may_reset(u):
            continue  # only users the caller may reset: editors' scope, class teachers' own students
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
            "as": "admin" if is_editor else "class_teacher",
            "usernames": [r[1] for r in rows],
            "filter": {"q": q, "role": role, "group_id": str(group_id) if group_id else None},
        },
        building_ids=_building_ids(acc.write_b or set()),
    )
    await ctx.commit()
    return _xlsx(write_users(rows, lang, credentials=True), f"{FILE_NAMES[lang][1]}-{date.today().isoformat()}.xlsx")


@router.get("/import-sample.xlsx", response_class=Response, responses={200: {"content": {XLSX_MIME: {}}}})
async def import_sample(lang: Lang = "en", ctx: Ctx = Depends(require(USERS_READ, any_room=True))):
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
    building_ids: list[uuid.UUID] = Query(default=[]),
    ctx: Ctx = Depends(require(USERS_WRITE, GROUPS_WRITE, any_room=True)),
):
    """Create students from an .xlsx (body = the raw file). Each gets their own
    one-time password (download them with GET /users/credentials.xlsx) and must choose
    a password at first login; usernames that already exist are skipped. Any invalid
    row rejects the whole file (422, detail = ImportResult). With dry_run=true nothing
    is written: use it to preview.

    Where the students go: a manager's import always goes to their own building(s)
    (`building_ids` may name a subset); an admin's or unit manager's goes to the given
    `building_ids` (repeat the parameter), or school-wide when there are none. Classes
    the file creates belong to the same buildings."""
    assert ctx.principal is not None
    # The buildings the caller may put users and classes into (None = the whole school).
    limit: set[uuid.UUID] | None = None
    for allowed in (ctx.principal.buildings_with(USERS_WRITE), ctx.principal.buildings_with(GROUPS_WRITE)):
        if allowed is not None:
            limit = allowed if limit is None else limit & allowed
    chosen = set(building_ids)
    if limit is not None:
        if chosen and not chosen <= limit:
            raise AppError(
                status.HTTP_403_FORBIDDEN, "users.out_of_scope", "that building is outside the buildings you manage"
            )
        chosen = chosen or limit
        if not chosen:
            raise AppError(status.HTTP_403_FORBIDDEN, "users.out_of_scope", "you manage no building to import into")
    elif chosen:
        found = set((await ctx.db.execute(select(Building.id).where(Building.org_id == ctx.org_id, Building.id.in_(chosen)))).scalars())
        if missing := chosen - found:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown buildings: {sorted(map(str, missing))}")
    require_secret_key()  # fail before the preview, not after
    data = await read_limited(request, MAX_IMPORT_BYTES)

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
    if limit is not None:
        # A manager can't put students into a class of another building (or a school-wide one).
        foreign = [n for n, g in groups.items() if not ({b.id for b in g.buildings} and {b.id for b in g.buildings} <= limit)]
        if foreign:
            result = ImportResult(
                dry_run=dry_run,
                errors=[
                    ImportIssue(
                        row=0,
                        error=f"class {n!r} exists outside your buildings",
                        code="users.import_group_out_of_scope",
                        params={"group": n},
                    )
                    for n in sorted(foreign, key=natural_key)
                ],
            )
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, result.model_dump())

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
    await ctx.db.flush()
    ctx.db.add_all(
        GroupBuilding(org_id=ctx.org_id, group_id=groups[name].id, building_id=b) for name in missing_groups for b in sorted(chosen)
    )
    scopes: list[uuid.UUID | None] = sorted(chosen) or [None]  # school-wide when no building
    users = [
        User(
            org_id=ctx.org_id,
            username=r.username,
            display_name=r.name,
            auth_source=AuthSource.LOCAL,
            roles=[RoleAssignment(org_id=ctx.org_id, role=Role.STUDENT, building_id=b) for b in scopes],
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
            "building_ids": sorted(map(str, chosen)),
        },
        building_ids=_building_ids(chosen),
    )
    await ctx.commit()
    return result


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, ctx: Ctx = Depends(require(USERS_READ, any_room=True))):
    acc = await _access(ctx)
    return _out(acc, await _load_user(ctx, user_id, acc))


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: uuid.UUID, body: UserUpdate, ctx: Ctx = Depends(require(USERS_WRITE, any_room=True))):
    acc = await _access(ctx)
    user = await _load_user(ctx, user_id, acc, edit=True)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("disabled") and user.id == ctx.principal.id:
        raise AppError(status.HTTP_400_BAD_REQUEST, "users.cannot_disable_self", "you cannot disable yourself")
    if "email" in changes:
        await _check_email_free(ctx, changes["email"], except_user=user.id)
    for k, v in changes.items():
        setattr(user, k, v)
    if changes.get("disabled"):
        await ctx.db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    ctx.audit("core.user.update", user, changes, building_ids=_building_ids(acc.user_buildings(user)))
    ctx.emit("core.user.updated", _event(user))
    await ctx.commit()
    return _out(acc, user)


@router.put("/{user_id}/roles", response_model=UserOut)
async def set_roles(user_id: uuid.UUID, body: list[RoleIn], ctx: Ctx = Depends(require(USERS_WRITE, any_room=True))):
    """Replace the user's role assignments. See `create_user` for what the caller may
    grant. Nobody changes their own roles, except an admin, who must keep their highest
    org-wide role."""
    acc = await _access(ctx)
    user = await _load_user(ctx, user_id, acc, edit=True)
    assignments = await _grant(ctx, acc, body)
    before = [(ra.role, ra.room_id, ra.building_id) for ra in user.roles]
    p = ctx.principal
    assert p is not None
    if p.kind == "user" and user.id == p.id:
        if "*" not in p.global_perms:
            raise AppError(status.HTTP_400_BAD_REQUEST, "users.cannot_change_own_roles", "you cannot change your own roles")
        own = _org_scoped(before)
        if own and max(own, key=role_table.rank) not in _org_scoped(assignments):
            raise AppError(
                status.HTTP_400_BAD_REQUEST, "users.cannot_remove_own_admin", "you cannot remove your own highest role"
            )
    if _org_scoped(before) & _token_managing_roles(ctx) and _org_perms_shrink(ctx, before, assignments):
        await _drop_tokens(ctx, user, "core.token.delete_with_admin_role")
    before_b = acc.user_buildings(user)
    # Delete first: the unit of work would otherwise insert before deleting and trip
    # the (user, role, room, building) unique constraint.
    await ctx.db.execute(delete(RoleAssignment).where(RoleAssignment.user_id == user.id))
    ctx.db.expire(user, ["roles"])
    ctx.db.add_all(
        RoleAssignment(org_id=ctx.org_id, user_id=user.id, role=role, room_id=room, building_id=b) for role, room, b in assignments
    )
    ctx.audit(
        "core.user.roles",
        user,
        {"roles": [{"role": r, "room_id": str(rm) if rm else None, "building_id": str(b) if b else None} for r, rm, b in assignments]},
        building_ids=_building_ids(before_b, acc.scope((rm, b) for _, rm, b in assignments)[0]),
    )
    ctx.emit("core.user.updated", _event(user))
    await ctx.commit()
    await ctx.db.refresh(user, ["roles"])
    return _out(acc, user)


@router.put("/{user_id}/groups", response_model=UserOut)
async def set_groups(
    user_id: uuid.UUID, group_ids: list[uuid.UUID], ctx: Ctx = Depends(require(GROUPS_WRITE, any_room=True))
):
    """Replace the user's class/group memberships (the user-side view of
    PUT /groups/{id}/members). A manager only touches groups of their buildings."""
    acc = await _access(ctx)
    # Whoever may edit users needs to be able to edit this one; a caller with only
    # the groups permission (no user rights at all) just needs to see them.
    user = await _load_user(ctx, user_id, acc, edit=acc.has_write)
    added, removed = await _set_groups(ctx, user, set(group_ids))
    ctx.audit(
        "core.user.groups",
        user,
        {"added": sorted(map(str, added)), "removed": sorted(map(str, removed))},
        building_ids=_building_ids(acc.user_buildings(user)),
    )
    await ctx.commit()
    await ctx.db.refresh(user, ["groups"])
    return _out(acc, user)


@router.post("/{user_id}/password", response_model=PasswordResetOut)
async def set_password(user_id: uuid.UUID, body: PasswordSet, ctx: Ctx = Depends(get_ctx)):
    """Reset: a given password (whoever may edit the user), or (body without password) a
    new one-time password (the same, or class teachers for their students). Either way
    all sessions end and the user must choose a new password."""
    p = ctx.principal
    assert p is not None
    acc = await _access(ctx)
    # Permission first, so callers without it can't probe which user ids exist.
    if not acc.has_write and not p.can(RESET_CLASS):
        raise AppError(
            status.HTTP_403_FORBIDDEN, "permission.missing", "missing permission core.users.write", permission="core.users.write"
        )
    if not acc.has_write and body.password is not None:
        raise AppError(
            status.HTTP_403_FORBIDDEN, "users.typed_password_admin_only", "only administrators can set a typed password"
        )
    user = await ctx.get(User, user_id)
    await acc.load_users([user])
    if user.auth_source != AuthSource.LOCAL:
        raise AppError(status.HTTP_400_BAD_REQUEST, "auth.external_password", "password is managed by an external directory")
    is_editor = acc.may_edit(user)
    if body.password is not None and not is_editor:
        raise AppError(
            status.HTTP_403_FORBIDDEN, "users.typed_password_admin_only", "only administrators can set a typed password"
        )
    if not acc.may_reset(user):
        if acc.has_write and acc.visible(user):
            raise AppError(
                status.HTTP_403_FORBIDDEN, "users.not_editable", "you may not change this user (outside your buildings or above your level)"
            )
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
        {"one_time_password": otp is not None, "as": "admin" if is_editor else "class_teacher"},
        building_ids=_building_ids(acc.user_buildings(user)),
    )
    await ctx.commit()
    return PasswordResetOut(one_time_password=otp, otp_expires_at=user.otp_expires_at)


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: uuid.UUID, ctx: Ctx = Depends(require(USERS_WRITE, any_room=True))):
    acc = await _access(ctx)
    user = await _load_user(ctx, user_id, acc, edit=True)
    if user.id == ctx.principal.id:
        raise AppError(status.HTTP_400_BAD_REQUEST, "users.cannot_delete_self", "you cannot delete yourself")
    removed = await remove_assignments_for(ctx, TargetType.USER, user.id)
    await _drop_tokens(ctx, user, "core.token.delete_with_user")
    ctx.audit(
        "core.user.delete",
        user,
        {"username": user.username, "removed_assignments": removed},
        building_ids=_building_ids(acc.user_buildings(user)),
    )
    ctx.emit("core.user.deleted", _event(user))
    if removed:
        emit_policy_changed(ctx, user_id=user.id)
    await ctx.db.delete(user)
    await ctx.commit()
