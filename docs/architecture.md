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
- Brute force (`smp/core/throttle.py`): failed logins are counted **per account, from any IP**: `SMP_LOGIN_MAX_FAILURES` (5) failures within 15 minutes lock that username for `SMP_LOGIN_LOCKOUT_SECONDS` (60 s); while locked, even the right password gets 429, so rotating source IPs doesn't help an attacker. Separately an IP gets at most `SMP_LOGIN_IP_ATTEMPTS_PER_MINUTE` attempts. Both are checked before any hashing or DB work, and both behave identically for existing and unknown usernames, so they're no enumeration oracle. The cost: anyone can keep an account locked by failing 5 times a minute, which is why the lockout is short (full lockouts / OTP resets are marked `TODO(evaluate)` in throttle.py). The state is in memory, per process. Behind a reverse proxy, uvicorn must see the real client IP (`--forwarded-allow-ips`). Argon2 runs in a worker thread with a concurrency cap, so a login flood can't stall the event loop or exhaust RAM. Every non-throttled failed attempt is audited, and the throttle is rebuilt from those audit rows on restart. See [security.md](security.md).
- **One-time passwords (OTPs)** (`smp/core/otp.py`): accounts someone else sets up get an *individual* random OTP (`xxxx-xxxx-xxxx`), never a shared default password. A shared one would let any student claim any not-yet-activated account.
  - **Where it comes from:** creating a user without a password, every imported student, and an admin reset without a password.
  - **Validity:** `core.otp_valid_days` (Beállítások). An expired OTP fails login, and the admin generates a new one.
  - **Storage:** the OTP's argon2 hash is the account password until first login. An encrypted copy (Fernet, key from `SMP_SECRET_KEY`) is kept so admins can export login slips (`GET /users/credentials.xlsx`: the import columns plus One-time password and Valid until (Egyszeri jelszó, Érvényes eddig with `?lang=hu`); admin-only and audited by username, never by value) until the OTP is used. Without `SMP_SECRET_KEY` these endpoints answer 503.
  - **Forced change:** any admin-set password (OTP or typed) sets `must_change_password`. Until it's changed, the server answers every endpoint except `/auth/me`, `/auth/password`, `/auth/logout` and `/modules` with `403 password change required`. Changing the password rotates the session (all of the user's sessions end and a new cookie is issued) and clears the OTP.
  - **Who sees activation state:** `must_change_password` and `otp_status` appear in user listings only for admins (`core.users.write`).
  - **The first admin too:** `./install.sh` runs `smp bootstrap --otp`, so the bootstrap admin also starts with a one-time password and must replace it at first sign-in. (`smp bootstrap` without `--otp` asks for a password instead and keeps it, for development.)
- **Class teachers** (`group_teachers`, set on the Csoportok page via `PUT /groups/{id}/teachers`) are teachers responsible for a group. They're kept separate from members, so they don't inherit the students' group policies or count as members. A class teacher (`core.users.reset_class_password`, granted to teachers by default) can issue a **new one-time password** (`POST /users/{id}/password` with an empty body) to users who are members of a group they teach and have only the student role. They can't do it for teachers, admins or themselves, and can't set typed passwords. The user list returns `can_reset_password` per row, and shows activation state to whoever may reset that account. Resets are audited with `"as": "class_teacher"`.
- Password policy (`smp/core/passwords.py`) for passwords people choose: at least 8 characters, letters and digits, at least 5 distinct characters, not a common password, and not containing the username. It applies to password changes, admin-typed passwords and `smp bootstrap`.
- User import/export (`server/smp/core/xlsx_users.py`): `.xlsx` with the columns **Name | Username | Class** (classes comma separated). Downloads take `?lang=en|hu` (the UI sends its language; Hungarian headers are **Név | Felhasználónév | Osztály**), and import accepts either header set.
  - `GET /users/export.xlsx` takes the same filters as `GET /users` and exports every matching user, not just one page. It's audited, and cells are forced to text so names can't act as spreadsheet formulas.
  - `POST /users/import` (raw file body, streamed with a 5 MB cap; `?dry_run=true` previews) creates **students**, each with their own one-time password. Download the OTPs right after the import, or later, with the credentials export. Existing usernames are skipped, missing classes are created, and any invalid row rejects the whole file (422 with per-row errors).
  - The sample files are [`docs/samples/users-sample.xlsx`](samples/users-sample.xlsx) (English) and [`docs/samples/felhasznalok-minta.xlsx`](samples/felhasznalok-minta.xlsx) (Hungarian), and the UI downloads the same content from `GET /users/import-sample.xlsx?lang=…`. A test keeps them identical.
- Identity providers: `User.auth_source` selects who verifies the password. `local` (argon2) exists now. The identity module adds `ldap`, `google` and `entra` by returning providers from `Module.identity_providers()`. Users are always mirrored locally so roles, groups and audit work the same for everyone.

### RBAC
- Permissions are strings `<module>.<thing>.<verb>` declared in module manifests, each with default roles. `admin` implicitly has all of them.
- Permissions about things that live in a room are marked `room_scoped=True` (`core.rooms.read`, `core.devices.read`, `core.policies.assign`, later screen lock etc.). A role assigned *for a room* grants its room-scoped permissions only in that room. Its other permissions (view users, create short links) apply org-wide. So "teacher in Lab 1" is a teacher everywhere but controls only Lab 1's machines.
- `require("perm")` checks for an org-wide grant. `require("perm", any_room=True)` accepts a grant for some room, and the handler must then filter or check per room (`ctx.principal.rooms_with(perm)`, `ctx.check(perm, room_id)`). Safe by default: forgetting the room check means room-scoped teachers are denied, not over-granted.

- Default role grants: teachers get read access plus room-scoped control. Students get no permissions at all, so the UI shows them only the home page and their own account (the "in development" placeholders are hidden from them too). Short links used to be open to students; they were removed because `/s/<code>` on the school's own domain made a convenient phishing tool. A new module decides per permission whether students get it.
- API tokens die with their creator's admin rights: deleting a user, or removing their admin role, deletes the tokens they created, including tokens those tokens created, and audits which ones (`core.token.delete_with_user` / `core.token.delete_with_admin_role`: id, name, permissions; never the token itself). Only admins can create tokens, so "no longer an admin" covers every way of losing that right.

### Audit
- `ctx.audit(action, target, details)` writes into the same transaction as the change, so there's no change without its audit row and no audit row for a rolled-back change.
- `audit_log` is append-only, enforced by a Postgres trigger. A future retention job deletes old rows after `SET LOCAL smp.audit_purge = 'on'`.
- Action naming: `<module>.<entity>.<verb>`, e.g. `classroom.screen.lock`.

### Events
- `ctx.emit(subject, payload)` queues an event, and `ctx.commit()` publishes it after the commit succeeds.
- Subjects use NATS syntax (`core.device.*`, `core.>`), so the in-process bus can be replaced by NATS when modules move to separate processes.

### DNS control-plane integration

The `dns` module is a normal control-plane module with PostgreSQL-backed
blocklist and allowlist entries and the `/api/v1/dns/*` endpoints. Browser actions
and hosts imports always identify the user on the server. `addedBy` is never
accepted from a client payload.

Decision order for a query (`smp/modules/dns/domains.py`, and `Match` in the Go
plugin): **allowlist → blocklist → allow everything else**. Public suffixes (com,
hu, co.uk, …, from the Public Suffix List bundled with `tldextract`, used offline)
are a built-in part of the allowlist: they can't be put on either list, because one
such entry would block or unblock a whole top-level domain. Domain names are
validated strictly (labels, length, no wildcards or single labels; IDN as punycode).
Total allowlist control (block everything else) is marked `TODO(evaluate)`.

After any change to either list, the API pushes the complete canonical JSON state
(`entries` and `allowlist`) to the Go service over `POST /api/blocklist/sync`. The Go
service replaces its in-memory lists and persists `blocklist.json`. The DNS hot path performs only
memory matching and never queries PostgreSQL or calls the API.

### Home page (Kezdőlap)
`GET /api/v1/dashboard` collects cards from every module's `dashboard(ctx)` hook, already filtered by the caller's permissions. A failing module only loses its own cards. The UI refreshes them every 10 s while the tab is visible (and immediately when it becomes visible again), and renders them generically: "stat" cards with a value, and "list" cards. Each card links to its page. The DNS module will contribute "top blocked domains" this way, and until it exists the UI shows a placeholder card.

### Languages
The API is English-only. The web UI is Hungarian or English (a per-browser HU | EN switch, Hungarian by default) and translates everything it shows: its own text, plus server text by stable keys (menu titles by route path, dashboard cards by widget id with the numbers in `Widget.data`, settings fields by name). Errors a user can see are raised as `AppError` (`smp/core/errors.py`): `{"detail": "<English>", "code": "...", "params": {...}}`, and the UI translates the code. The .xlsx import/export files use the UI language's column headers (`?lang=`), and import accepts both. Details: [writing-a-module.md](writing-a-module.md) (section 6) and `WEBUI/readme.md`.

### Module registry
See [writing-a-module.md](writing-a-module.md). Modules are compiled in (listed in `server/smp/modules/__init__.py`) and can be switched off per install with `SMP_ENABLED_MODULES`. `GET /api/v1/modules` returns every manifest (permissions, UI routes, JSON schemas for settings and policies), so the UI can build navigation and settings forms from it.

## Deployment

`./install.sh` sets up `.env`, runs `docker compose up -d --build` (nginx, Postgres, the server, the DNS sinkhole) and, on first setup, `smp bootstrap --otp` with the school name and admin username it asks for. The server image builds the web UI too, so UI and API share one origin (no CORS, the session cookie just works). On every start the container runs `smp preflight` (secret key usable? database login works? if not, it prints what's wrong and how to fix it instead of a traceback, and waits 30 s before exiting so the restart loop stays slow and readable), then `smp migrate`, then the server.

**nginx** (`nginx/`, an image built on `nginx:1.30.7-alpine`) is the only web entry point:

- **Routing by name:** `SMP_SERVER_NAMES` → the portal (HTTP redirects to HTTPS); any other name → the DNS sinkhole's block page (port 80, and 443 after the browser's certificate warning). A name that is neither SMP's nor blocked gets a short "unknown address" page.
- **TLS:** on start the container creates a self-signed certificate for exactly `SMP_SERVER_NAMES` (renewed when the names change) unless `nginx/certs/` holds a certificate someone else issued, which is never touched. No HSTS while the certificate is self-signed, or browsers wouldn't let users past the warning.
- **Hardening:** login and general rate limits, `nosniff`, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `Referrer-Policy`, 64 MB upload limit (imports).
- **Client IPs:** nginx overwrites `X-Forwarded-For` with the real client address; the server trusts it only from SMP's docker network (`SMP_DOCKER_SUBNET`, fixed so it can be trusted), so audit IPs and the login throttle see real clients and nobody can fake theirs.
- The server (8000) and the block page/sync API (8080) are not published; only nginx (80/443) and DNS (1053) are.

## Not done yet (intentionally)

- Agent enrolment, mTLS gateway, command signing (agent module)
- Per-org overrides of role → permission mappings (defaults come from manifests)
- Roles granted via groups (e.g. everyone in "Staff" is a teacher). Planned with directory sync.
- Audit retention job (setting `core.audit_retention_days` exists)
- OIDC login, MFA
