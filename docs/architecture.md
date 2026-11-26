# SMP architecture

## Components and languages

| Component | Language | Why |
|---|---|---|
| **Control plane** (`server/`): core + modules, REST API | Python (FastAPI, SQLAlchemy 2, Alembic, Postgres) | CRUD/RBAC/business logic; fastest for the team to extend; OpenAPI is generated automatically |
| **Web UI** (`WEBUI/`) | Vue 3 + TypeScript (Vite) | API types are generated from the server's OpenAPI; built into the server image and served on the same origin |
| **DNS sinkhole** (`Site-blocker/school-dns-demo/`) | Go, CoreDNS with a custom plugin | Mature DNS server; stays up when the control plane is down because it caches the blocklist locally |
| **Agent** (`agent/`) | Rust | Single static `.exe` (cross-compiled on Linux), Windows service + per-session helper for screen capture and the tray icon; no async runtime, plain threads |

Rule: the data plane (DNS, agent, and later the screen relay if Python can't keep up) runs as separate programs that talk to the control plane with API tokens (DNS) or signed requests (agent). Anything that is mostly "store, check permissions, audit" is a Python module.

### Versions (`Version.toml`)

Every version number lives in `Version.toml` in the repository root, one section per component (`[server]`, `[WEBUI]`, `[agent]`, `[Site-blocker]`) plus `[release]` for SMP as a whole. To bump a version, edit that file only.

**Which number:** `[server]`, `[WEBUI]` and `[agent]` carry the release's number (`1.0.3` in release 1.0.3); during development their revision letter counts up (`1.0.3 A`, `1.0.3 B`, ...), so each component reads as up to date (equal to `[release]`), ahead (in development) or behind. `tools/version.sh --status` lists them, and a test fails if one of them has another number than `[release]`. Server modules report `[server]` too (they're built with it). `[Site-blocker]` is versioned on its own by the DNS filter's maintainer. Agents update to whatever differs from their own version, so on a development server every letter reaches the PCs. The format is `"1.0.2"` or `"1.0.2 C"` (an optional revision letter).

| Component | Reads it | Where it shows |
|---|---|---|
| server | `smp/version.py` at startup (`SMP_VERSION_FILE`, else the nearest `Version.toml` above the package; the image sets `/app/Version.toml`) | API docs, `/healthz`, the core module's version (sidebar), the pip metadata (`pyproject.toml` is `dynamic`, `1.0.2 C` becomes `1.0.2+c`) |
| web UI | `vite.config.ts` at build time, as `__SMP_WEBUI_VERSION__` | sidebar tooltip, next to the server's |
| agent | `smp-agent/build.rs` at build time, as `SMP_AGENT_VERSION` (`Cargo.toml` has no version) | `smp-agent --version`, heartbeats (Devices), `smp-agent.version` beside the download |
| DNS sinkhole | its Dockerfile, via `go build -ldflags -X` | startup log, `/api/blocklist/status` |

Shell scripts and Docker stages read it with `tools/version.sh <section>`. The non-Python readers are line based, so each `version = "..."` stays on one line. `server/tests/test_version.py` checks the format, checks that `tools/version.sh` matches a real TOML parser, and fails if a component gets its own version back.

## Shared domain model (`server/smp/core/models.py`)

Every module speaks in these terms. Don't create a module-private "computer" or "classroom" table; reference `devices` / `rooms`.

```
Organization (a school / "unit"; every table is scoped to one)
 ├── User ──< GroupMember >── Group        (people, classes, staff lists)
 │                             └──< GroupBuilding >── Building  (none = school-wide)
 ├── RoleAssignment (user, role, optional scope: one room OR one building)
 ├── Building ──< Room (has subnets) ──< Device   (how teachers think about machines)
 ├── Policy (kind = module name, data validated by that module's schema;
 │           optional owner building, NULL = school-wide)
 │    └──< PolicyAssignment ──> target (org | building | room | group | device | user)
 │                          └──> Schedule (optional time windows)
 ├── ModuleSetting (per-org settings for each module)
 ├── ApiToken / AuthSession                (machine + browser credentials)
 └── AuditLog                              (append-only; building_ids = who may see it)
```

(This is the diagram at the top of `models.py`; keep the two in step.)

- **Organization**: the "unit": a school today. Every table has `org_id`, so one install can host several units later, one subdomain per unit (planned, and it depends on the domain-tunnel work in `plans/domain-tunnel-roadmap.md`). The roles and scopes below are built so that split needs data changes, not new `if` branches.
- **Building**: groups rooms, and is the scope a manager administers. A room is in one building or in none; rooms, devices and join requests with no building are "unit level" (only the unit-wide roles reach them). Migration 0017 turned the old free-text `rooms.building` into buildings (one per distinct trimmed text); the room API keeps a read-only `building` name next to `building_id`.
- **User**, **Group** (class "10.B", staff, ...), **GroupMember**. A group can belong to several buildings (`group_buildings`); none means school-wide.
- **RoleAssignment**: `(user, role, room?, building?)`: at most one of room and building, none = the whole unit. Roles are `admin`, `unit_manager`, `manager`, `teacher`, `student`; which role may sit at which scope is a table (see "Roles and scopes"). A user "belongs" to the buildings of their assignments (a building scope, or the building of a room scope).
- **Room**: physical location in a building, with `subnets` (CIDRs), so DNS can map an IP to a room even without an agent.
- **Device**: hostname, MAC, IP (`last_ip`: its own, as its agent reports it; `observed_ip`: where SMP sees its connection come from, different behind a NAT), room, agent key + fingerprint, `room_mode` (`fixed`, or `network`: the room follows the IP), logged-in user, OS info. Its building is its room's.
- **Policy**: `(kind, name, data)` with an optional owner building (NULL = school-wide, edited by unit-level roles only). `kind` is a module name (`dns`, `classroom`, ...), and `data` is validated by that module's `policy_model`.
- **PolicyAssignment**: policy → target (`org` / `building` / `room` / `group` / `device` / `user`), with optional `schedule`, `starts_at`/`ends_at`, `priority`, `label`.
- **Schedule**: weekly time windows in a timezone (lesson times); like policies, optionally owned by a building.

### Policy resolution (`server/smp/core/policy.py`)

For a device (and/or user) at time *t*, per policy kind:

1. Candidates: assignments for the org, the building of the device's room, the device's room, the device, the user, and the user's groups.
2. Keep those that are enabled, inside `[starts_at, ends_at)`, and inside a schedule window if they have one.
3. Winner: highest `priority`, then the most specific target (device/user > room/group > building > org), then the newest.

This gives you:
- **Default policy**: org assignment, priority 0.
- **Lesson-time policy**: room assignment + schedule.
- **Exam mode**: room assignments for several kinds (dns + classroom) with `priority: 100`, `ends_at`, and the same `label`. Room-scoped teachers are allowed to create these for their own rooms.
- **Building policy**: a building assignment covers every room of that building; a building's manager can set one without touching the rest of the school.

`PolicyAssignment.target_id` points at different tables, so it has no foreign key. Deleting a building, room, device, group or user therefore removes the assignments that target it in the same transaction (`remove_assignments_for()`). A schedule that assignments still use can't be deleted (409), because removing it would silently turn "only during lessons" into "always".

`resolve_ip()` maps a client IP to a device (agent-reported `last_ip`), falling back to the room whose subnet contains it (longest prefix wins). Every change that can affect resolution emits `core.policy.changed` (use `emit_policy_changed()`): assignments, schedules, a device changing rooms, a room changing buildings, a group's buildings, room subnets, group membership, and deleting any assignment target.

## Core services

### Auth
- Browser: `POST /api/v1/auth/login` sets an HttpOnly, `SameSite=Strict` session cookie. Only the SHA-256 of the token is stored, so sessions are revocable. Cross-origin unsafe requests are rejected as CSRF defence.
- Services (DNS container, gateway, scripts): API tokens (`Authorization: Bearer smpt_...`) with an explicit permission list. Create them with `smp create-token` or `POST /api/v1/tokens` (`core.tokens.manage`: unit managers and admins). A token can't hold more than its creator, and below an admin you can revoke only tokens that hold nothing you lack.
- Brute force (`smp/core/throttle.py`): failed logins are counted **per account, from any IP**: `SMP_LOGIN_MAX_FAILURES` (5) failures within 15 minutes lock that username for `SMP_LOGIN_LOCKOUT_SECONDS` (60 s); while locked, even the right password gets 429, so rotating source IPs doesn't help an attacker. Separately an IP gets at most `SMP_LOGIN_IP_ATTEMPTS_PER_MINUTE` attempts. Both are checked before any hashing or DB work, and both behave identically for existing and unknown usernames, so they're no enumeration oracle. The cost: anyone can keep an account locked by failing 5 times a minute, which is why the lockout is short (full lockouts / OTP resets are marked `TODO(evaluate)` in throttle.py). The state is in memory, per process. Behind a reverse proxy, uvicorn must see the real client IP (`--forwarded-allow-ips`). Argon2 runs in a worker thread with a concurrency cap, so a login flood can't stall the event loop or exhaust RAM. Every non-throttled failed attempt is audited, and the throttle is rebuilt from those audit rows on restart. See [security.md](security.md).
- **One-time passwords (OTPs)** (`smp/core/otp.py`): accounts someone else sets up get an *individual* random OTP (`xxxx-xxxx-xxxx`), never a shared default password. A shared one would let any student claim any not-yet-activated account.
  - **Where it comes from:** creating a user without a password, every imported student, and a reset without a password by someone who may edit that user.
  - **Validity:** `core.otp_valid_days` (Settings). An expired OTP fails login, and whoever manages the account generates a new one.
  - **Storage:** the OTP's argon2 hash is the account password until first login. An encrypted copy (Fernet, key from `SMP_SECRET_KEY`) is kept so those who manage accounts can export login slips (`GET /users/credentials.xlsx`: the import columns plus One-time password and Valid until (Egyszeri jelszó, Érvényes eddig with `?lang=hu`); only for users the caller may reset (a manager: those of their buildings) and audited by username, never by value) until the OTP is used. Without `SMP_SECRET_KEY` these endpoints answer 503.
  - **Forced change:** any password set by someone else (OTP or typed) sets `must_change_password`. Until it's changed, the server answers every endpoint except `/auth/me`, `/auth/password`, `/auth/logout` and `/modules` with `403 password change required`. Changing the password rotates the session (all of the user's sessions end and a new cookie is issued) and clears the OTP.
  - **Who sees activation state:** `must_change_password` and `otp_status` appear in user listings only for whoever may reset that account (those who may edit the user, and class teachers for their students).
  - **The first admin too:** `./install.sh` runs `smp bootstrap --otp`, so the bootstrap admin also starts with a one-time password and must replace it at first sign-in. (`smp bootstrap` without `--otp` asks for a password instead and keeps it, for development.)
- **Class teachers** (`group_teachers`, set on the Groups page via `PUT /groups/{id}/teachers`) are teachers responsible for a group (any user holding a role that includes the teacher's powers qualifies: teacher, manager, unit manager). They're kept separate from members, so they don't inherit the students' group policies or count as members. A class teacher (`core.users.reset_class_password`, granted to teachers by default) can issue a **new one-time password** (`POST /users/{id}/password` with an empty body) to users who are members of a group they teach and have only the student role. They can't do it for staff accounts or themselves, and can't set typed passwords. The user list returns `can_reset_password` per row, and shows activation state to whoever may reset that account. Resets are audited with `"as": "class_teacher"`.
- Password policy (`smp/core/passwords.py`) for passwords people choose: at least 8 characters, letters and digits, at least 5 distinct characters, not a common password, and not containing the username. It applies to password changes, admin-typed passwords and `smp bootstrap`.
- User import/export (`server/smp/core/xlsx_users.py`): `.xlsx` with the columns **Name | Username | Class** (classes comma separated). Downloads take `?lang=en|hu` (the UI sends its language; Hungarian headers are **Név | Felhasználónév | Osztály**), and import accepts either header set.
  - `GET /users/export.xlsx` takes the same filters as `GET /users` and exports every matching user, not just one page. It's audited, and cells are forced to text so names can't act as spreadsheet formulas.
  - `POST /users/import` (raw file body, streamed with a 5 MB cap; `?dry_run=true` previews) creates **students**, each with their own one-time password, in the importing manager's building(s), or in the `building_ids` an admin or unit manager names (none: school-wide); classes the file creates belong to the same buildings, and a manager can't fill a class of another building. Download the OTPs right after the import, or later, with the credentials export. Existing usernames are skipped, missing classes are created, and any invalid row rejects the whole file (422 with per-row errors).
  - The sample files are [`docs/samples/users-sample.xlsx`](samples/users-sample.xlsx) (English) and [`docs/samples/felhasznalok-minta.xlsx`](samples/felhasznalok-minta.xlsx) (Hungarian), and the UI downloads the same content from `GET /users/import-sample.xlsx?lang=…`. A test keeps them identical.
- Identity providers: `User.auth_source` selects who verifies the password. `local` (argon2) exists now. The identity module adds `ldap`, `google` and `entra` by returning providers from `Module.identity_providers()`. Users are always mirrored locally so roles, groups and audit work the same for everyone.

### RBAC

#### Roles and scopes (`server/smp/core/roles.py`)
Roles are a table (`RoleDef`: rank, scopes, inherits, grants), not `if role == ...` in handlers, so a later instance-level role or a new unit role is a row. ("Unit" in the table is the `org` scope in the code.)

| Role | Rank | Assignable at | Inherits | May grant | Covers |
|---|---|---|---|---|---|
| `admin` | 100 | unit | - | every role | everything (wildcard) |
| `unit_manager` | 80 | unit | `manager` | manager, teacher, student | the whole unit: rooms, devices and join requests with no building, school-wide groups and policies, buildings, API tokens, software uploads |
| `manager` | 60 | building | `teacher` | teacher, student | the assigned building(s): users, rooms, devices, groups, policies, building audit, software assignments |
| `teacher` | 40 | unit, building, room | - | - | read access plus room-scoped control (all rooms, a building's rooms, or one room) |
| `student` | 10 | unit, building | - | - | nothing (own account) |

- **Inheritance:** a role also gets the default permissions of the roles it inherits, transitively (`ModuleRegistry.role_permissions`). A module declares a permission for the *lowest* role that should have it; managers inherit everything teachers have, unit managers everything managers have. `admin` is the wildcard.
- **Granting:** the table says which roles a holder may give; the endpoint adds the scope check (a manager of building A makes teachers and students only in building A). The role must be assignable at the chosen scope (`allowed_scope`: a manager only per building, an admin only unit-wide).
- **Admin-only, even for unit managers:** module settings, `/api/docs` and `openapi.json`, and creating admins or unit managers.
- Users belong to buildings through their assignments (see "Domain model"), so a manager's world is: users with an assignment in their buildings, those buildings' rooms, devices, groups and policies, and the audit entries about them. Another building answers 404, as if it didn't exist.

#### Permissions and how an assignment applies them
Permissions are strings `<module>.<thing>.<verb>` declared in module manifests (`Permission` in `core/module.py`), each with default roles. There are three kinds:

| Kind | Examples | Org assignment | Room assignment | Building assignment |
|---|---|---|---|---|
| `room_scoped` (things in a room) | `core.rooms.read`, `core.devices.read`, `core.policies.assign`, `classroom.screens.*`, `software.status.read` | everywhere | that room only | every room of that building |
| `org_wide` (school-wide data) | DNS lists, own short links, `core.users.reset_class_password` | everywhere | everywhere | everywhere |
| neither, "building-level" (the admin-type powers) | `core.users.write`, `core.groups.write`, `core.rooms.write`, `core.devices.write`, `core.policies.write`, `core.audit.read`, `software.assignments.write`, `core.buildings.read` | everywhere | everywhere (legacy) | **that building only** |

- A **room assignment** keeps its old meaning: room-scoped permissions in that room, everything else org-wide. So "teacher in Lab 1" is a teacher everywhere but controls only Lab 1's machines.
- A **building assignment** never turns a building-level permission into an org-wide one. This is the fail-closed rule: a permission someone forgot to classify is confined to the building, not widened to the school.
- `Principal` (`core/context.py`) holds the result: `global_perms`, `room_perms` (a building's room-scoped permissions are expanded to its rooms when the principal is built, so a room moved into a building is covered at once), `building_perms`, and `room_buildings` (room → building, so a building grant can be checked for a room).
  - `can(perm)`: org-wide only. `can(perm, room_id=r)`: org-wide, granted for `r`, or granted for `r`'s building. `can(perm, building_id=b)`: org-wide or granted for `b`.
  - `can_anywhere(perm)`, `rooms_with(perm)` and `buildings_with(perm)` say where it applies; the last two return `None` for "everywhere", which is what list endpoints filter on.
- `require("perm")` checks for an org-wide grant, so a building-scoped holder is denied. `require("perm", any_room=True)` accepts a grant for some room or building, and the handler **must** then filter or check per room or building (`rooms_with`, `buildings_with`, `ctx.check(perm, room_id=..., building_id=...)`). Safe by default: forgetting the check means scoped users are denied, not over-granted.

#### Who may do what with users, groups, rooms and policies
- **Users:** a manager sees the users with any assignment in their buildings and edits (update, delete, roles, groups, password reset, credentials export) only those whose assignments *all* lie inside their buildings and whose roles they may grant; a user with an assignment elsewhere, or none, is out of reach. Users a manager creates or imports go to the manager's building(s) (a role without a scope becomes one assignment per building); an admin's or unit manager's imports name the buildings or go school-wide. Nobody changes their own roles, except an admin, who must keep their highest unit-wide role. `core/api/users.py`.
- **Groups:** a group lists its buildings; managers see and edit groups with a building among theirs (editing needs *all* of the group's buildings to be theirs), and see only the members and teachers they can see. School-wide groups need the unit level. `core/api/groups.py`.
- **Rooms:** managers create, edit and delete rooms of their buildings, and moving a room checks both buildings. Setting **subnets** needs the unit level: the IP → room mapping is school-wide (devices whose room follows the network, DNS, agents with no room), so a broad subnet on one building's room could pull other buildings' machines into it.
- **Policies and schedules:** an owner building (NULL = school-wide). Managers create and edit their buildings' and may *assign* school-wide ones to their building (and see them for that); school-wide ones are edited at unit level. Assignment rules (`core/api/policies.py`): the org target needs an org-wide assign grant; room and device targets need `core.policies.assign` for the room (teachers: their rooms; managers: their buildings' rooms); a building target needs `core.policies.write` for that building; a group or user needs the same where the group lies in buildings the caller manages entirely, or the user's assignments all do. Assignment lists show only targets inside the caller's rooms and buildings (this also closed a leak: room teachers used to see every assignment).
- **Buildings:** reading is scoped (own buildings); create, rename and delete need `core.buildings.write` (unit managers, admins). Deleting is refused (409) while the building has rooms, policies or schedules.
- **DNS** stays school-wide on purpose: its permissions are `org_wide`, and teachers and up use them from any scope, until the domain-tunnel work reworks it.

#### Default role grants
Teachers get read access plus room-scoped control; managers add the building's administration; unit managers the unit's. Students get no permissions at all, so the UI shows them only the home page and their own account. Short links used to be open to students; they were removed because `/s/<code>` on the school's own domain made a convenient phishing tool. A new module decides per permission whether students get it.

#### API tokens
Tokens die with their creator's rights: deleting a user who can manage tokens (`core.tokens.manage`: admin, unit manager), or changing their roles so that they lose any org-wide permission (a role removed, or lowered, e.g. admin to unit manager), deletes the tokens they created, including tokens those tokens created, and audits which ones (`core.token.delete_with_user` / `core.token.delete_with_admin_role`: id, name, permissions; never the token itself). Managers, like teachers, can't create tokens, so "loses an org-wide permission" covers every way a token could outlive a right its creator had. Tokens are org-wide `global_perms` only: they have no building scope.

### Audit
- `ctx.audit(action, target, details)` writes into the same transaction as the change, so there's no change without its audit row and no audit row for a rolled-back change.
- `audit_log` is append-only, enforced by a Postgres trigger. A future retention job deletes old rows after `SET LOCAL smp.audit_purge = 'on'`.
- Action naming: `<module>.<entity>.<verb>`, e.g. `classroom.lock_logo.update`.
- **Which buildings an entry is about** (`audit_log.building_ids`, a `uuid[]` with a GIN index) decides who reads it: org-wide readers see everything, building managers the entries that overlap their buildings (`GET /audit`, the home page's recent activity). Entries with NULL there (failed logins, other unit-level actions, and everything written before buildings existed: no backfill, the table is append-only) are for the unit level only, and the failed-login card is shown only to them.
  - `ctx.audit(..., building_ids=[...])` takes an explicit list. Otherwise `Ctx.audit` derives it from the target: a Building is its own; an object with `building_id` (room, policy, ...) has that one; one with `room_id` (device, software assignment) gets its room's building from a subquery inside the INSERT.
  - It is filled in **when the row is inserted**, never with an UPDATE: the append-only trigger would reject that. So code that moves something between buildings passes both the old and the new building.

### Events
- `ctx.emit(subject, payload)` queues an event, and `ctx.commit()` publishes it after the commit succeeds.
- Subjects use NATS syntax (`core.device.*`, `core.>`), so the in-process bus can be replaced by NATS when modules move to separate processes.

### DNS control-plane integration

The `dns` module is a normal control-plane module with PostgreSQL-backed
blocklist and allowlist entries and the `/api/v1/dns/*` endpoints. Browser actions
and hosts imports always identify the user on the server. `addedBy` is never
accepted from a client payload.

Decision order for a query (`smp/modules/dns/domains.py`, and `Match` in the Go
plugin): **allowlist → built-in blocklist → blocklist → allow everything else**. Public suffixes (com,
hu, co.uk, …, from the Public Suffix List bundled with `tldextract`, used offline)
are a built-in part of the allowlist: they can't be put on either list, because one
such entry would block or unblock a whole top-level domain. The built-in part of the
blocklist (`BUILTIN_BLOCKLIST` in `domains.py`; the sinkhole has the same list, and a
test keeps them equal) holds public DNS-over-HTTPS servers and iCloud Private Relay,
which would let a device resolve names around SMP; an allowlist entry overrides it.
Imports read hosts files, plain domain lists and Adblock-style `||domain^` lists;
allow rules (`@@`), withdrawn rules (`$badfilter`) and element-hiding rules
(`example.com##…`, which name the site the rule works on, not one to block) are skipped. Domain names are
validated strictly (labels, length, no wildcards or single labels; IDN as punycode).
Total allowlist control (block everything else) is marked `TODO(evaluate)`.

After any change to either list, the API pushes the complete canonical JSON state
(`entries` and `allowlist`) to the Go service over `POST /api/blocklist/sync`. The Go
service replaces its in-memory lists and persists `blocklist.json`. The DNS hot path performs only
memory matching and never queries PostgreSQL or calls the API.

### Home page
`GET /api/v1/dashboard` collects cards from every module's `dashboard(ctx)` hook, already filtered by the caller's permissions. A failing module only loses its own cards. Cards are built per caller and must not count or list other buildings' data: a manager's user count, temporary policies and recent activity cover only their buildings. The UI refreshes them every 10 s while the tab is visible (and immediately when it becomes visible again), and renders them generically: "stat" cards with a value, and "list" cards. Each card links to its page (e.g. the DNS blocklist's size, short links, the software module's installation states).

### Languages
The API is English-only. The web UI is Hungarian or English (a per-browser HU | EN switch, Hungarian by default) and translates everything it shows: its own text, plus server text by stable keys (menu titles by route path, dashboard cards by widget id with the numbers in `Widget.data`, settings fields by name). Errors a user can see are raised as `AppError` (`smp/core/errors.py`): `{"detail": "<English>", "code": "...", "params": {...}}`, and the UI translates the code. The .xlsx import/export files use the UI language's column headers (`?lang=`), and import accepts both. Details: [writing-a-module.md](writing-a-module.md) (section 6) and `WEBUI/readme.md`.

### Module registry
See [writing-a-module.md](writing-a-module.md). Modules are compiled in (listed in `server/smp/modules/__init__.py`) and can be switched off per install with `SMP_ENABLED_MODULES`. `GET /api/v1/modules` returns every manifest (permissions, UI routes, JSON schemas for settings and policies), so the UI can build navigation and settings forms from it.

### Agents (`server/smp/modules/agent/`, `agent/`)
- **Identity:** each PC makes an ECDSA P-256 key on install. Its fingerprint (SHA-256 of the public key) is the device's identity; the IP is not (it changes, laptops move between VLANs). There are no tokens or passwords: every request is signed (`X-SMP-Key`, `X-SMP-Time`, `X-SMP-Signature` over method, path, time and body hash; 5 minutes' clock skew; replayed signatures refused). The format is described in `agent/smp-agent/src/core/identity.rs` and `server/smp/modules/agent/crypto.py`, which must agree.
- **Joining:** the installer asks for SMP's address (and optionally the building: `--building <id or name>`) and sends a join request; an admin (or the manager of the PC's building, see below) approves it in Devices by comparing the fingerprint the PC printed, as a new device or as an existing one (MAC/hostname suggestion), into a fixed room or "by network". Rejected requests are remembered (by key and MAC, until "allow again"), so a rejected machine doesn't come back after an IP change or a reinstall; expired ones vanish. A request matching an online PC isn't preselected as that PC, and taking an online PC over needs an explicit confirmation. Identity reset (Devices → edit) makes the PC ask again.
  - **Building hint:** the join body has an optional `building` (an id or a name; the agent protocol is otherwise unchanged, and older agents simply send none). The server stores it as `building_hint` and resolves `building_id` (by id, else by case-insensitive name), updating both on every repeated join while the request is pending. It is only a hint: whoever approves still picks the room.
  - **Who sees what:** a request with a building is shown to that building's managers, who approve only into rooms of their buildings (a fixed room of theirs, or "by network" when the address lands in one of their rooms; taking over an existing device only if it is in their buildings). Requests with no (or no matching) building are for the unit level. Identity reset is device-scoped the same way.
  - **Install command:** `GET /agent/install-command?building_id=` returns `smp-agent.exe install --server <url> --building <id>` for the Devices page: preset for a manager (their building), a choice (or none) for unit-level roles.
- **Heartbeat = long poll:** the agent sends its config version; the server answers when the config changes (`AgentHub` wakes waiting agents on policy, room, device and `agent.config.changed` events) or after `SMP_AGENT_LONGPOLL_SECONDS` (25). No DB connection is held while waiting. Each heartbeat updates the IP (and, for `network` devices, the room), the logged-in user and OS info.
- **Addresses:** the agent reports its own address, the one its system uses to reach SMP, because behind a NAT (a lab with its own router) SMP only sees the router. That becomes the device's IP (`last_ip`); the address SMP sees is kept as `observed_ip` (the Devices page shows "via ..." when they differ). A `network` device's room is the one whose subnet holds its own address, else the seen one, so a NATted lab can be set up by its inner subnet or by its router's address. Agents before 1.0.3 report none: then both are the seen address. The per-address limit on pending join requests uses the seen address, since a reported one is only the agent's word.
- **Config:** `Module.agent_config(db, device)` hooks; each module returns its section (`{"classroom": {...}}`), and the agent hands each section to its capability (`Capability::section()`; several capabilities may read one module's section: screen monitoring and the screen lock both read `classroom`). New agent features = a module hook + a Rust capability; the core on both sides stays the same.
- **TLS:** the agent trusts a certificate the OS trusts, or the one it pinned on first contact (SMP's nginx starts self-signed). A different, untrusted certificate is refused.
- **Updates:** the config's `agent` section carries `update` (version, size, SHA-256, path) for the build the server has for the agent's platform (`download.py`; the image builds Windows). An agent whose version differs installs it by itself (`capabilities/update.rs`): random delay up to `update_spread_s`, download over the pinned TLS, size + hash + `--version` check, then on Windows an exe swap and a watcher process that restarts the service and undoes the update if the new version doesn't reach the server within 3 minutes (`platform/win/update.rs`). `update.json` next to `agent.json` is the bookkeeping between them. Admins switch it off in the agent settings.
- **Reconnecting:** every error is retried (backoff up to 30 s), a heartbeat without an answer after 60 s counts as an error, network errors reset the connection pool, and a watchdog restarts the agent after 15 minutes without contact (`core/watchdog.rs`).

### Monitoring (`server/smp/modules/classroom/`)
- PCs send nothing unless someone has the room's Monitoring page open: the page sends a keep-alive every ≤5 s, a viewer without one for 20 s times out. Only the PC a teacher enlarged sends full-size pictures.
- Pictures are JPEGs the agent shrinks itself (default 480 px wide, quality 60, every 5 s; enlarged: 1600 px every 2 s), set per school (Settings) and per room/schedule (policy kind `classroom`, e.g. slower on weak Wi-Fi, or `enabled: false` outside lessons). A screen that didn't change is confirmed with an empty request.
- Frames live in server memory only (latest per PC), are dropped when nobody watches, and are never written to disk or the database. Who watched which room, whom they enlarged and when they stopped is in the audit log.
- Seating plan per room (`classroom_layouts`): automatic order by hostname, or a rows × columns grid that teachers with `classroom.layout.edit` arrange.
- One server process holds the viewer and frame state. Several server replicas would need it in Redis/NATS; see "Not done yet".
- **Screen lock** (`classroom.screens.lock`, room-scoped): teachers lock all or some PCs of a room with a message. Locks are stored (`classroom_locks`: one per PC, with the room it was locked in, so a server restart doesn't unlock and a PC that leaves the room is free) and reach the PC as `"lock": {"message", "language", "logo"}` inside its `classroom` config section; the server always fills in the text (the teacher's, else the school's suggestion from Settings, else a built-in sentence in the block page's language, `dns.block_page_language`), so no PC's own Windows language decides it; unlocked PCs get no `lock` key, so their config doesn't change. The logo (`classroom_lock_logos`, one PNG per school, uploaded in Settings; the web UI converts any image to a PNG of at most 1024 px) is named in the config only by SHA-256 and size; locked PCs fetch it once with a signed `GET /api/v1/classroom/agent/lock-logo`. No logo: the SMP logo built into the agent. On the PC: `capabilities/lock/` (a capability whose `section()` is `classroom`); on Windows the session helper shows a topmost window over every monitor and blocks keyboard and mouse with low-level hooks. The lock ends whenever the agent stops (fail-open). Monitoring policies don't affect it.
- **Remote control** (`classroom.screens.control`, room-scoped): a teacher sees one PC's screen live and uses its mouse and keyboard. Transport: a WebSocket between the browser and SMP (`WS /api/v1/classroom/devices/{id}/remote`: input as JSON batches every 40 ms; each new picture as a JSON message with its size followed by the JPEG), signed HTTP between SMP and the agent (`PUT /agent/remote/frame`, and `GET /agent/remote/input`, a long poll answered as soon as there's input). Sessions live in memory (`classroom/remote.py`), one per PC, and last exactly as long as the WebSocket; the PC gets `"remote": {"session", "interval_ms", ...}` in its `classroom` section meanwhile and stops when it's gone. Agents report their capabilities in each heartbeat (`devices.agent_capabilities`), so the button appears only for agents that can do it. A locked PC can't be controlled, and locking ends control. The teacher can block the student's own keyboard and mouse for the session (`{"block_input": true}` on the WebSocket → `"block_local"` in the PC's config): the helper's hooks then let through only its own injected events, recognised by a random stamp only that SYSTEM process knows; an agent without contact to SMP for 30 s lifts the block itself. nginx: a WebSocket location, and its own rate limit for the agent's remote endpoints (several requests a second from one PC). Keystrokes are never stored; start and end (who, which PC, how long) are audited.

### Software deployment (`server/smp/modules/software/`)
- **Packages:** a unit manager or admin (`software.packages.write`) uploads an installer (`POST /software/uploads`, the raw file as the body; `.msi` or `.exe`, checked by extension *and* first bytes), then describes it: silent-install arguments, how to tell it's installed (`.msi`: its product code, read from the file by the agent unless given; `.exe`: a registry key/value under HKLM or a file, required), how to remove it (`.msi`: automatic; `.exe`: an uninstall command, else it can't be removed), and **restart**: `never` (default; a needed restart shows as `reboot_pending`), `if_required` (the installer's exit code says so: 3010, 1641) or `always`. A new file for an existing package is a new version: computers that have it install it again (they compare SHA-256).
- **Files** are streamed to `SMP_SOFTWARE_DIR` (the `softwaredata` volume), stored once per SHA-256, never in memory or the database. Uploads nobody's package uses (also the old file of a package that got a new one, and a deleted package's) are removed by the first clean-up a day or more after they were uploaded; clean-ups run at server start and after each upload. nginx passes this one endpoint through unbuffered and without its 64 MB limit; the server's limit is `SMP_SOFTWARE_MAX_UPLOAD_BYTES` (8 GiB).
- **Assignments:** package → the whole school, a building, a room or one computer, `install` or `remove`. A computer's building is its room's. Per package and computer the most specific one wins (computer > room > building > school), so "everywhere except PC-07" works. Deleting an assignment leaves the program installed; only `remove` uninstalls (otherwise laptops moving between rooms would uninstall and reinstall, and one deleted row could wipe a program school-wide).
- **Not a policy kind, on purpose:** room-scoped teachers can attach any existing policy to their room (`core.policies.assign`, exam mode), and installing software runs code as SYSTEM on the computers, so only the unit level decides what can run: *what* a package contains (uploads, `software.packages.write`) is unit managers' and admins' say, and managers (`software.assignments.write`) only choose which of those existing packages reach their own buildings (never the whole school, and they can't add installers of their own). The installer itself still runs as SYSTEM on every computer it reaches, so a manager's reach is exactly their buildings' computers; teachers get no say. Policies also pick one winner per kind (a room's policy would replace the school's software baseline instead of adding to it), and schedules would install and uninstall programs every lesson. Teachers get `software.status.read` for their rooms (state per computer, home-page card).
- **To the computers:** `agent_config()` sends each computer its list (section `software`: per package the action, file size + SHA-256, download path, arguments, detection, restart, timeout; plus `spread_s` and `reboot_warning_s` from the settings). The agent downloads from `GET /software/agent/packages/{id}`: signed, only for a computer the package is assigned to (installers can contain license keys, so this isn't public like the agent exe), resumable (`Range`). At most `max_parallel_downloads` computers download at once (Settings; default 5, about what a 100 Mbps network carries); the others get `503` with a randomized `Retry-After`.
- **Reports:** `POST /software/agent/status` with per-package states (`waiting`, `downloading`, `installing`, `installed`, `reboot_pending`, `failed`, `removing`, `removed`, `remove_failed`), the file's SHA-256, exit code and a message. A report counts only for the current file and direction, otherwise the computer shows `pending`.
- **On the PC** (`agent/smp-agent/src/capabilities/software/`): a worker thread goes through the list shortly after it changes (random wait up to `spread_s`) and every 30 minutes, one package at a time. Per package: the detection rule decides (installed + the file this agent installed = nothing to do; installed but not by SMP = taken over as is; installed with an older file = upgrade; not installed = install), then download (signed, resumable, SHA-256 checked), run without a window or shell (`msiexec /i <file> /qn /norestart /l*v <log>` + the admin's arguments, or the .exe with its arguments), interpret the exit code (0; 3010/1641 = restart needed; 1618 = another installation running, retry; the package's extra success codes), check the rule again, report. Failures wait 1, 2, 4, ... 24 hours unless the package changes. A restart (`always`, or `if_required` and the installer said so) is asked of Windows once per boot with `shutdown.exe /r /t <reboot_warning_s>` and a message (30 s when nobody is logged on). `software.json` next to `agent.json` remembers installed files, .msi product codes, failures and pending restarts. On Linux `SMP_AGENT_FAKE_SOFTWARE=1` pretends (test PCs).

## Deployment

`install.sh` downloads SMP into its folder (main branch on GitHub, or `--ref` / `--download-url`; not in a git checkout, whose files it uses as they are), hands over to the installer that came with it (`--local`), sets up `.env`, runs `docker compose up -d --build` (nginx, Postgres, the server, the DNS sinkhole) and, on first setup, `smp bootstrap --otp` with the school name and admin username it asks for. The server image builds the web UI too, so UI and API share one origin (no CORS, the session cookie just works). It also cross-compiles the Windows agent (stage `agent`) and serves it at `/downloads/smp-agent.exe` (public: the exe holds no secrets) with its SHA-256 on the Devices page, so every install hands out the agent matching its own version and nothing comes from outside the school's server. On every start the container runs `smp preflight` (secret key usable? database login works? if not, it prints what's wrong and how to fix it instead of a traceback, and waits 30 s before exiting so the restart loop stays slow and readable), then `smp migrate`, then the server.

**Upgrades** (`upgrade.sh`): downloads SMP the same way (`install.sh --fetch`), compares `[release]` (`tools/version.sh --compare`: numbers, then the revision letter), lists what changes, then replaces the files with `tools/release.sh` from the *new* release and runs its `install.sh --local`. The install folder's `.smp-manifest` (SHA-256 of every file the installed release brought) tells removed files apart from the school's own, and hand-edited files from untouched ones; every replaced or removed file is kept in `backups/<time>-from-<version>/`. `.env`, `nginx/certs/` and `backups/` are never written, whatever a release contains: the PC agents pin the self-signed certificate (`agent/smp-agent/src/core/tls.rs`), so a new one would cut off every installed agent. Images are built while the old containers keep running; `docker compose up` then swaps them, so DNS (the school's only resolver) and the agents (they retry, backing off to 30 s) are interrupted for seconds, not for the build. Files are written next to the old ones and renamed into place, so a running `upgrade.sh` keeps reading its own old copy. Before the files change, the database is backed up encrypted (`database.smpbackup`, `SMP_BACKUP_KEY`; details in [security.md](security.md)); `upgrade.sh --rollback` puts the files back, builds the old version while the current one runs, then stops the server and restores the database. A failed upgrade does the same by itself, importing the database only if the new version had already migrated it (`db-revision` in the backup vs. `alembic_version`). After a successful upgrade only the newest backup is kept.

**nginx** (`nginx/`, an image built on `nginx:1.30.7-alpine`) is the only web entry point:

- **Routing by name:** `SMP_SERVER_NAMES` → the portal (HTTP redirects to HTTPS); any other name → the DNS sinkhole's block page (port 80, and 443 after the browser's certificate warning). A name that is neither SMP's nor blocked gets a short "unknown address" page.
- **TLS:** on start the container creates a self-signed certificate for exactly `SMP_SERVER_NAMES` (renewed when the names change) unless `nginx/certs/` holds a certificate someone else issued, which is never touched. No HSTS while the certificate is self-signed, or browsers wouldn't let users past the warning.
- **Hardening:** login and general rate limits, `nosniff`, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `Referrer-Policy`, 64 MB upload limit (imports).
- **Client IPs:** nginx overwrites `X-Forwarded-For` with the real client address; the server trusts it only from SMP's docker network (`SMP_DOCKER_SUBNET`, fixed so it can be trusted), so audit IPs and the login throttle see real clients and nobody can fake theirs.
- The server (8000) and the block page/sync API (8080) are not published; only nginx (80/443) and DNS (port 53 on the LAN address `SMP_SINKHOLE_IP`; CoreDNS listens on 1053 inside its container) are. The DNS container keeps the last synced lists on the `dnsdata` volume.

## Not done yet (intentionally)

- Agent: key in the TPM, automatic DNS setting on install; an MSI/installer around `smp-agent install`; software packages that are more than one file (a folder with setup.exe), MSIX/AppX, per-user (HKCU) installs
- Monitoring with several server processes (viewer/frame state is per process)
- Several units on one instance (routing by subdomain, an instance-level role above `admin`; the role table and `org_id` scoping are prepared for it)
- Per-org overrides of role → permission mappings (defaults come from manifests)
- Roles granted via groups (e.g. everyone in "Staff" is a teacher). Planned with directory sync.
- Audit retention job (setting `core.audit_retention_days` exists)
- OIDC login, MFA
