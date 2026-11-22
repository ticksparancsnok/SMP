# SMP architecture

## Components and languages

| Component | Language | Why |
|---|---|---|
| **Control plane** (`server/`): core + modules, REST API | Python (FastAPI, SQLAlchemy 2, Alembic, Postgres) | CRUD/RBAC/business logic; fastest for the team to extend; OpenAPI is generated automatically |
| **Web UI** (`WEBUI/`) | Vue 3 + TypeScript (Vite) | API types are generated from the server's OpenAPI; built into the server image and served on the same origin |
| **DNS sinkhole** (`Site-blocker/school-dns-demo/`) | Go, CoreDNS with a custom plugin | Mature DNS server; stays up when the control plane is down because it caches the blocklist locally |
| **Agent** (`agent/`, planned, after the core is stable) | Rust | Single static `.exe`, Windows service + per-session helper for screen capture and lock |

Rule: the data plane (DNS, agent, and later the screen relay if Python can't keep up) runs as separate programs that talk to the control plane with API tokens / mTLS. Anything that is mostly "store, check permissions, audit" is a Python module.

## Shared domain model (`server/smp/core/models.py`)

Every module speaks in these terms. Don't create a module-private "computer" or "classroom" table; reference `devices` / `rooms`.

- **Organization**: a school. Every table has `org_id`, so one install can host several schools later.
- **User**, **Group** (class "10.B", staff, ...), **GroupMember**
- **RoleAssignment**: `(user, role, room?)`. Roles are `admin`, `teacher`, `student`. A teacher can be assigned org-wide or only to specific rooms.
- **Room**: physical location with `subnets` (CIDRs), so DNS can map an IP to a room even without an agent.
- **Device**: hostname, MAC, last IP, room, agent ID/cert fingerprint, OS info.
- **Policy**: `(kind, name, data)`. `kind` is a module name (`dns`, `software`, `classroom`...), and `data` is validated by that module's `policy_model`.
- **PolicyAssignment**: policy → target (`org` / `room` / `group` / `device` / `user`), with optional `schedule`, `starts_at`/`ends_at`, `priority`, `label`.
- **Schedule**: weekly time windows in a timezone (lesson times).

### Policy resolution (`server/smp/core/policy.py`)

For a device (and/or user) at time *t*, per policy kind:

1. Candidates: assignments for the org, the device's room, the device, the user, and the user's groups.
2. Keep those that are enabled, inside `[starts_at, ends_at)`, and inside a schedule window if they have one.
3. Winner: highest `priority`, then the most specific target (device/user > room/group > org), then the newest.

This gives you:
- **Default policy**: org assignment, priority 0.
- **Lesson-time policy**: room assignment + schedule.
- **Exam mode**: room assignments for several kinds (dns + classroom + software) with `priority: 100`, `ends_at`, and the same `label`. Room-scoped teachers are allowed to create these for their own rooms.

`PolicyAssignment.target_id` points at different tables, so it has no foreign key. Deleting a room, device, group or user therefore removes the assignments that target it in the same transaction (`remove_assignments_for()`). A schedule that assignments still use can't be deleted (409), because removing it would silently turn "only during lessons" into "always".

`resolve_ip()` maps a client IP to a device (agent-reported `last_ip`), falling back to the room whose subnet contains it (longest prefix wins). Every change that can affect resolution emits `core.policy.changed` (use `emit_policy_changed()`): assignments, schedules, a device changing rooms, room subnets, group membership, and deleting any assignment target.

## Core services

### Auth
- Browser: `POST /api/v1/auth/login` sets an HttpOnly, `SameSite=Strict` session cookie. Only the SHA-256 of the token is stored, so sessions are revocable. Cross-origin unsafe requests are rejected as CSRF defence.
- Services (DNS container, gateway, scripts): API tokens (`Authorization: Bearer smpt_...`) with an explicit permission list. Create them with `smp create-token` or `POST /api/v1/tokens`. A token can't hold more than its creator.
- Brute force (`smp/core/throttle.py`): no per-account lockout (that would let anyone lock out the admin). Failed logins are counted per **(client IP, username)**: after `SMP_LOGIN_MAX_FAILURES` within `SMP_LOGIN_LOCKOUT_MINUTES` that IP gets 429 for that username, and an IP gets at most `SMP_LOGIN_IP_ATTEMPTS_PER_MINUTE` attempts. Both are checked before any hashing or DB work, and both behave identically for existing and unknown usernames, so they're no enumeration oracle. The state is in memory, per process. Behind a reverse proxy, uvicorn must see the real client IP (`--forwarded-allow-ips`). Argon2 runs in a worker thread with a concurrency cap, so a login flood can't stall the event loop or exhaust RAM. Every non-throttled failed attempt is audited, and the throttle is rebuilt from those audit rows on restart. See [security.md](security.md).
- **One-time passwords (OTPs)** (`smp/core/otp.py`): accounts someone else sets up get an *individual* random OTP (`xxxx-xxxx-xxxx`), never a shared default password. A shared one would let any student claim any not-yet-activated account.
  - **Where it comes from:** creating a user without a password, every imported student, and an admin reset without a password.
  - **Validity:** `core.otp_valid_days` (Beállítások). An expired OTP fails login, and the admin generates a new one.
  - **Storage:** the OTP's argon2 hash is the account password until first login. An encrypted copy (Fernet, key from `SMP_SECRET_KEY`) is kept so admins can export login slips (`GET /users/credentials.xlsx`: the import columns plus Egyszeri jelszó and Érvényes eddig; admin-only and audited by username, never by value) until the OTP is used. Without `SMP_SECRET_KEY` these endpoints answer 503.
  - **Forced change:** any admin-set password (OTP or typed) sets `must_change_password`. Until it's changed, the server answers every endpoint except `/auth/me`, `/auth/password`, `/auth/logout` and `/modules` with `403 password change required`. Changing the password rotates the session (all of the user's sessions end and a new cookie is issued) and clears the OTP.
  - **Who sees activation state:** `must_change_password` and `otp_status` appear in user listings only for admins (`core.users.write`).
  - **Exempt:** the bootstrap admin from `smp bootstrap` chose their own password.
- **Class teachers** (`group_teachers`, set on the Csoportok page via `PUT /groups/{id}/teachers`) are teachers responsible for a group. They're kept separate from members, so they don't inherit the students' group policies or count as members. A class teacher (`core.users.reset_class_password`, granted to teachers by default) can issue a **new one-time password** (`POST /users/{id}/password` with an empty body) to users who are members of a group they teach and have only the student role. They can't do it for teachers, admins or themselves, and can't set typed passwords. The user list returns `can_reset_password` per row, and shows activation state to whoever may reset that account. Resets are audited with `"as": "class_teacher"`.
- Password policy (`smp/core/passwords.py`) for passwords people choose: at least 8 characters, letters and digits, at least 5 distinct characters, not a common password, and not containing the username. It applies to password changes, admin-typed passwords and `smp bootstrap`.
- User import/export (`server/smp/core/xlsx_users.py`): `.xlsx` with the columns **Név | Felhasználónév | Osztály** (classes comma separated).
  - `GET /users/export.xlsx` takes the same filters as `GET /users` and exports every matching user, not just one page. It's audited, and cells are forced to text so names can't act as spreadsheet formulas.
  - `POST /users/import` (raw file body, streamed with a 5 MB cap; `?dry_run=true` previews) creates **students**, each with their own one-time password. Download the OTPs right after the import, or later, with the credentials export. Existing usernames are skipped, missing classes are created, and any invalid row rejects the whole file (422 with per-row errors).
  - The sample file is [`docs/samples/felhasznalok-minta.xlsx`](samples/felhasznalok-minta.xlsx), and the UI downloads the same content from `GET /users/import-sample.xlsx`. A test keeps the two identical.
- Identity providers: `User.auth_source` selects who verifies the password. `local` (argon2) exists now. The identity module adds `ldap`, `google` and `entra` by returning providers from `Module.identity_providers()`. Users are always mirrored locally so roles, groups and audit work the same for everyone.

### RBAC
- Permissions are strings `<module>.<thing>.<verb>` declared in module manifests, each with default roles. `admin` implicitly has all of them.
- Permissions about things that live in a room are marked `room_scoped=True` (`core.rooms.read`, `core.devices.read`, `core.policies.assign`, later screen lock etc.). A role assigned *for a room* grants its room-scoped permissions only in that room. Its other permissions (view users, create short links) apply org-wide. So "teacher in Lab 1" is a teacher everywhere but controls only Lab 1's machines.
- `require("perm")` checks for an org-wide grant. `require("perm", any_room=True)` accepts a grant for some room, and the handler must then filter or check per room (`ctx.principal.rooms_with(perm)`, `ctx.check(perm, room_id)`). Safe by default: forgetting the room check means room-scoped teachers are denied, not over-granted.

- Default role grants: teachers get read access plus room-scoped control. Students only get `shortlinks.links.create`, so the UI shows them Kezdőlap, Saját fiók and Rövid linkek and nothing else (the "Fejlesztés alatt" placeholders are hidden from them too). A new module decides per permission whether students get it.

### Audit
- `ctx.audit(action, target, details)` writes into the same transaction as the change, so there's no change without its audit row and no audit row for a rolled-back change.
- `audit_log` is append-only, enforced by a Postgres trigger. A future retention job deletes old rows after `SET LOCAL smp.audit_purge = 'on'`.
- Action naming: `<module>.<entity>.<verb>`, e.g. `classroom.screen.lock`.

### Events
- `ctx.emit(subject, payload)` queues an event, and `ctx.commit()` publishes it after the commit succeeds.
- Subjects use NATS syntax (`core.device.*`, `core.>`), so the in-process bus can be replaced by NATS when modules move to separate processes.

### DNS control-plane integration

The `dns` module is a normal control-plane module with PostgreSQL-backed
blocklist entries and the `/api/v1/dns/*` endpoints. Browser actions and hosts
imports always identify the user on the server. `addedBy` is never accepted from
a client payload.

After a blocklist mutation, the API pushes the complete canonical JSON state to
the Go service over `POST /api/blocklist/sync`. The Go service replaces its
in-memory blocklist and persists `blocklist.json`. The DNS hot path performs only
memory matching and never queries PostgreSQL or calls the API.

### Home page (Kezdőlap)
`GET /api/v1/dashboard` collects cards from every module's `dashboard(ctx)` hook, already filtered by the caller's permissions. A failing module only loses its own cards. The UI refreshes them every 10 s while the tab is visible (and immediately when it becomes visible again), and renders them generically: "stat" cards with a value, and "list" cards. Each card links to its page. The DNS module will contribute "top blocked domains" this way, and until it exists the UI shows a placeholder card.

### Module registry
See [writing-a-module.md](writing-a-module.md). Modules are compiled in (listed in `server/smp/modules/__init__.py`) and can be switched off per install with `SMP_ENABLED_MODULES`. `GET /api/v1/modules` returns every manifest (permissions, UI routes, JSON schemas for settings and policies), so the UI can build navigation and settings forms from it.

## Deployment

`docker compose up -d --build` runs Postgres and the server. The server image builds the web UI too, so UI and API share one origin (no CORS, the session cookie just works). The server applies migrations on start. Then run once:

```
docker compose exec server smp bootstrap --org-name "My School"
```

Production: put a TLS reverse proxy (Caddy/nginx) in front and set `SMP_COOKIE_SECURE=true` (the default outside compose). Run uvicorn with `--forwarded-allow-ips` set to the proxy so audit IPs are real.

## Not done yet (intentionally)

- Agent enrolment, mTLS gateway, command signing (agent module)
- Per-org overrides of role → permission mappings (defaults come from manifests)
- Roles granted via groups (e.g. everyone in "Staff" is a teacher). Planned with directory sync.
- Audit retention job (setting `core.audit_retention_days` exists)
- OIDC login, MFA
