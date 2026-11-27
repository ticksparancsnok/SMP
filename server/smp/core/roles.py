# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""The role table: what each role is, where it can be assigned and what it may hand out.

Everything about roles that is not a permission list lives here as data, so no code
needs `if role == "manager"`. The hierarchy it describes:

    Organization (the "unit" of the product; later several units per instance, each
    on its own subdomain) -> Building -> Room -> Device

    role          rank  assignable at         inherits     may grant
    admin          100  org                   -            every role
    unit_manager    80  org                   manager      manager, teacher, student
    manager         60  building              teacher      teacher, student
    teacher         40  org, building, room   -            -
    student         10  org, building         -            -

- "org" means the whole unit (today the whole organization). A future split into an
  instance-level admin and unit-level roles is made here: add the instance role (scope
  "instance") above `admin` and let `admin` become a unit role.
- `inherits`: the role also gets the default permissions of those roles
  (transitively), so a manager is a teacher plus the manager powers.
- `grants`: the roles a holder may give to others, inside the holder's own scope
  (a manager of building A may make teachers only in building A). The scope check is
  the endpoint's job; this table only says which roles may be given at all.
- `rank`: higher is more powerful. Compare ranks to answer "may this user be changed
  by that user" without naming roles.

This module has no database code.
"""

from dataclasses import dataclass

SCOPE_ORG = "org"
SCOPE_BUILDING = "building"
SCOPE_ROOM = "room"
SCOPE_KINDS = (SCOPE_ORG, SCOPE_BUILDING, SCOPE_ROOM)

ADMIN = "admin"
UNIT_MANAGER = "unit_manager"
MANAGER = "manager"
TEACHER = "teacher"
STUDENT = "student"


@dataclass(frozen=True)
class RoleDef:
    name: str
    rank: int  # higher = more powerful
    scopes: frozenset[str]  # where it may be assigned: "org", "building", "room"
    inherits: tuple[str, ...]  # roles whose default permissions this role also gets
    grants: frozenset[str]  # roles a holder may give to others (inside the holder's own scope)


ROLE_DEFS: dict[str, RoleDef] = {
    d.name: d
    for d in (
        RoleDef(ADMIN, 100, frozenset({SCOPE_ORG}), (), frozenset({ADMIN, UNIT_MANAGER, MANAGER, TEACHER, STUDENT})),
        RoleDef(
            UNIT_MANAGER,
            80,
            frozenset({SCOPE_ORG}),
            (MANAGER,),
            frozenset({MANAGER, TEACHER, STUDENT}),
        ),
        RoleDef(MANAGER, 60, frozenset({SCOPE_BUILDING}), (TEACHER,), frozenset({TEACHER, STUDENT})),
        RoleDef(TEACHER, 40, frozenset({SCOPE_ORG, SCOPE_BUILDING, SCOPE_ROOM}), (), frozenset()),
        RoleDef(STUDENT, 10, frozenset({SCOPE_ORG, SCOPE_BUILDING}), (), frozenset()),
    )
}


def role_def(name: str) -> RoleDef | None:
    """The definition of a role, None for an unknown name."""
    return ROLE_DEFS.get(name)


def allowed_scope(role: str, scope_kind: str) -> bool:
    """May `role` be assigned at this kind of scope ("org" | "building" | "room")?"""
    d = ROLE_DEFS.get(role)
    return d is not None and scope_kind in d.scopes


def can_grant(granter_role: str, role: str) -> bool:
    """May a holder of `granter_role` give `role` to others?"""
    d = ROLE_DEFS.get(granter_role)
    return d is not None and role in d.grants


def rank(role: str) -> int:
    """How powerful a role is (0 for an unknown name)."""
    d = ROLE_DEFS.get(role)
    return d.rank if d else 0


def with_inherited(role: str) -> list[str]:
    """The role and every role it inherits from, transitively (the role itself first)."""
    seen: list[str] = []
    todo = [role]
    while todo:
        name = todo.pop(0)
        if name in seen:
            continue
        seen.append(name)
        d = ROLE_DEFS.get(name)
        if d:
            todo.extend(d.inherits)
    return seen
