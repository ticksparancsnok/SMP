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

- **Organization**: a school. Every table has `org_id`, so one install can host several schools later.
- **User**, **Group** (class "10.B", staff, ...), **GroupMember**
- **RoleAssignment**: `(user, role, room?)`. Roles are `admin`, `teacher`, `student`. A teacher can be assigned org-wide or only to specific rooms.
- **Room**: physical location with `subnets` (CIDRs), so DNS can map an IP to a room even without an agent.
- **Device**: hostname, MAC, IP (`last_ip`: its own, as its agent reports it; `observed_ip`: where SMP sees its connection come from, different behind a NAT), room, agent key + fingerprint, `room_mode` (`fixed`, or `network`: the room follows the IP), logged-in user, OS info.
- **Policy**: `(kind, name, data)`. `kind` is a module name (`dns`, `classroom`, ...), and `data` is validated by that module's `policy_model`.
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
- **Exam mode**: room assignments for several kinds (dns + classroom) with `priority: 100`, `ends_at`, and the same `label`. Room-scoped teachers are allowed to create these for their own rooms.

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

### Agents (`server/smp/modules/agent/`, `agent/`)
- **Identity:** each PC makes an ECDSA P-256 key on install. Its fingerprint (SHA-256 of the public key) is the device's identity; the IP is not (it changes, laptops move between VLANs). There are no tokens or passwords: every request is signed (`X-SMP-Key`, `X-SMP-Time`, `X-SMP-Signature` over method, path, time and body hash; 5 minutes' clock skew; replayed signatures refused). The format is described in `agent/smp-agent/src/core/identity.rs` and `server/smp/modules/agent/crypto.py`, which must agree.
- **Joining:** the installer asks only for SMP's address and sends a join request; an admin approves it in Devices by comparing the fingerprint the PC printed, as a new device or as an existing one (MAC/hostname suggestion), into a fixed room or "by network". Rejected requests are remembered (by key and MAC, until "allow again"), so a rejected machine doesn't come back after an IP change or a reinstall; expired ones vanish. A request matching an online PC isn't preselected as that PC, and taking an online PC over needs an explicit confirmation. Identity reset (Devices → edit) makes the PC ask again.
- **Heartbeat = long poll:** the agent sends its config version; the server answers when the config changes (`AgentHub` wakes waiting agents on policy, room, device and `agent.config.changed` events) or after `SMP_AGENT_LONGPOLL_SECONDS` (25). No DB connection is held while waiting. Each heartbeat updates the IP (and, for `network` devices, the room), the logged-in user and OS info.
- **Addresses:** the agent reports its own address, the one its system uses to reach SMP, because behind a NAT (a lab with its own router) SMP only sees the router. That becomes the device's IP (`last_ip`); the address SMP sees is kept as `observed_ip` (the Devices page shows "via ..." when they differ). A `network` device's room is the one whose subnet holds its own address, else the seen one, so a NATted lab can be set up by its inner subnet or by its router's address. Agents before 1.0.3 report none: then both are the seen address. The per-address limit on pending join requests uses the seen address, since a reported one is only the agent's word.
- **Config:** `Module.agent_config(db, device)` hooks; each module returns its section (`{"classroom": {...}}`), and the agent hands each section to its capability (`Capability::section()`; several capabilities may read one module's section: screen monitoring and the screen lock both read `classroom`). New agent features = a module hook + a Rust capability; the core on both sides stays the same.
- **TLS:** the agent trusts a certificate the OS trusts, or the one it pinned on first contact (SMP's nginx starts self-signed). A different, untrusted certificate is refused.
- **Updates:** the config's `agent` section carries `update` (version, size, SHA-256, path) for the build the server has for the agent's platform (`download.py`; the image builds Windows). An agent whose version differs installs it by itself (`capabilities/update.rs`): random delay up to `update_spread_s`, download over the pinned TLS, size + hash + `--version` check, then on Windows an exe swap and a watcher process that restarts the service and undoes the update if the new version doesn't reach the server within 3 minutes (`platform/win/update.rs`). `update.json` next to `agent.json` is the bookkeeping between them. Admins switch it off in the agent settings.
- **Reconnecting** (issue #10): every error is retried (backoff up to 30 s), a heartbeat without an answer after 60 s counts as an error, network errors reset the connection pool, and a watchdog restarts the agent after 15 minutes without contact (`core/watchdog.rs`).

### Monitoring (`server/smp/modules/classroom/`)
- PCs send nothing unless someone has the room's Monitoring page open: the page sends a keep-alive every ≤5 s, a viewer without one for 20 s times out. Only the PC a teacher enlarged sends full-size pictures.
- Pictures are JPEGs the agent shrinks itself (default 480 px wide, quality 60, every 5 s; enlarged: 1600 px every 2 s), set per school (Settings) and per room/schedule (policy kind `classroom`, e.g. slower on weak Wi-Fi, or `enabled: false` outside lessons). A screen that didn't change is confirmed with an empty request.
- Frames live in server memory only (latest per PC), are dropped when nobody watches, and are never written to disk or the database. Who watched which room, whom they enlarged and when they stopped is in the audit log.
- Seating plan per room (`classroom_layouts`): automatic order by hostname, or a rows × columns grid that teachers with `classroom.layout.edit` arrange.
- One server process holds the viewer and frame state. Several server replicas would need it in Redis/NATS; see "Not done yet".
- **Screen lock** (`classroom.screens.lock`, room-scoped): teachers lock all or some PCs of a room with a message. Locks are stored (`classroom_locks`: one per PC, with the room it was locked in, so a server restart doesn't unlock and a PC that leaves the room is free) and reach the PC as `"lock": {"message", "language", "logo"}` inside its `classroom` config section; the server always fills in the text (the teacher's, else the school's suggestion from Settings, else a built-in sentence in the block page's language, `dns.block_page_language`), so no PC's own Windows language decides it; unlocked PCs get no `lock` key, so their config doesn't change. The logo (`classroom_lock_logos`, one PNG per school, uploaded in Settings; the web UI converts any image to a PNG of at most 1024 px) is named in the config only by SHA-256 and size; locked PCs fetch it once with a signed `GET /api/v1/classroom/agent/lock-logo`. No logo: the SMP logo built into the agent. On the PC: `capabilities/lock/` (a capability whose `section()` is `classroom`); on Windows the session helper shows a topmost window over every monitor and blocks keyboard and mouse with low-level hooks. The lock ends whenever the agent stops (fail-open). Monitoring policies don't affect it.

### Software deployment (`server/smp/modules/software/`)
- **Packages:** an admin uploads an installer (`POST /software/uploads`, the raw file as the body; `.msi` or `.exe`, checked by extension *and* first bytes), then describes it: silent-install arguments, how to tell it's installed (`.msi`: its product code, read from the file by the agent unless given; `.exe`: a registry key/value under HKLM or a file, required), how to remove it (`.msi`: automatic; `.exe`: an uninstall command, else it can't be removed), and **restart**: `never` (default; a needed restart shows as `reboot_pending`), `if_required` (the installer's exit code says so: 3010, 1641) or `always`. A new file for an existing package is a new version: computers that have it install it again (they compare SHA-256).
- **Files** are streamed to `SMP_SOFTWARE_DIR` (the `softwaredata` volume), stored once per SHA-256, never in memory or the database. Uploads nobody's package uses (also the old file of a package that got a new one, and a deleted package's) are removed by the first clean-up a day or more after they were uploaded; clean-ups run at server start and after each upload. nginx passes this one endpoint through unbuffered and without its 64 MB limit; the server's limit is `SMP_SOFTWARE_MAX_UPLOAD_BYTES` (8 GiB).
- **Assignments:** package → the whole school, a room or one computer, `install` or `remove`. Per package and computer the most specific one wins (computer > room > school), so "everywhere except PC-07" works. Deleting an assignment leaves the program installed; only `remove` uninstalls (otherwise laptops moving between rooms would uninstall and reinstall, and one deleted row could wipe a program school-wide).
- **Not a policy kind, on purpose:** room-scoped teachers can attach any existing policy to their room (`core.policies.assign`, exam mode), and installing software runs code as SYSTEM on the computers, so only admins (`software.packages.write`, `software.assignments.write`) decide. Policies also pick one winner per kind (a room's policy would replace the school's software baseline instead of adding to it), and schedules would install and uninstall programs every lesson. Teachers get `software.status.read` for their rooms (state per computer, home-page card).
- **To the computers:** `agent_config()` sends each computer its list (section `software`: per package the action, file size + SHA-256, download path, arguments, detection, restart, timeout; plus `spread_s` and `reboot_warning_s` from the settings). The agent downloads from `GET /software/agent/packages/{id}`: signed, only for a computer the package is assigned to (installers can contain license keys, so this isn't public like the agent exe), resumable (`Range`). At most `max_parallel_downloads` computers download at once (Settings; default 5, about what a 100 Mbps network carries); the others get `503` with a randomized `Retry-After`.
- **Reports:** `POST /software/agent/status` with per-package states (`waiting`, `downloading`, `installing`, `installed`, `reboot_pending`, `failed`, `removing`, `removed`, `remove_failed`), the file's SHA-256, exit code and a message. A report counts only for the current file and direction, otherwise the computer shows `pending`.
- **On the PC** (`agent/smp-agent/src/capabilities/software/`): a worker thread goes through the list shortly after it changes (random wait up to `spread_s`) and every 30 minutes, one package at a time. Per package: the detection rule decides (installed + the file this agent installed = nothing to do; installed but not by SMP = taken over as is; installed with an older file = upgrade; not installed = install), then download (signed, resumable, SHA-256 checked), run without a window or shell (`msiexec /i <file> /qn /norestart /l*v <log>` + the admin's arguments, or the .exe with its arguments), interpret the exit code (0; 3010/1641 = restart needed; 1618 = another installation running, retry; the package's extra success codes), check the rule again, report. Failures wait 1, 2, 4, ... 24 hours unless the package changes. A restart (`always`, or `if_required` and the installer said so) is asked of Windows once per boot with `shutdown.exe /r /t <reboot_warning_s>` and a message (30 s when nobody is logged on). `software.json` next to `agent.json` remembers installed files, .msi product codes, failures and pending restarts. On Linux `SMP_AGENT_FAKE_SOFTWARE=1` pretends (test PCs).

## Deployment

`install.sh` downloads SMP into its folder (main branch on GitHub, or `--ref` / `--download-url`; not in a git checkout, whose files it uses as they are), hands over to the installer that came with it (`--local`), sets up `.env`, runs `docker compose up -d --build` (nginx, Postgres, the server, the DNS sinkhole) and, on first setup, `smp bootstrap --otp` with the school name and admin username it asks for. The server image builds the web UI too, so UI and API share one origin (no CORS, the session cookie just works). It also cross-compiles the Windows agent (stage `agent`) and serves it at `/downloads/smp-agent.exe` (public: the exe holds no secrets) with its SHA-256 on the Devices page, so every install hands out the agent matching its own version and nothing comes from outside the school's server. On every start the container runs `smp preflight` (secret key usable? database login works? if not, it prints what's wrong and how to fix it instead of a traceback, and waits 30 s before exiting so the restart loop stays slow and readable), then `smp migrate`, then the server.

**Upgrades** (`upgrade.sh`): downloads SMP the same way (`install.sh --fetch`), compares `[release]` (`tools/version.sh --compare`: numbers, then the revision letter), lists what changes, then replaces the files with `tools/release.sh` from the *new* release and runs its `install.sh --local`. The install folder's `.smp-manifest` (SHA-256 of every file the installed release brought) tells removed files apart from the school's own, and hand-edited files from untouched ones; every replaced or removed file is kept in `backups/<time>-from-<version>/`. `.env`, `nginx/certs/` and `backups/` are never written, whatever a release contains: the PC agents pin the self-signed certificate (`agent/smp-agent/src/core/tls.rs`), so a new one would cut off every installed agent. Images are built while the old containers keep running; `docker compose up` then swaps them, so DNS (the school's only resolver) and the agents (they retry, backing off to 60 s) are interrupted for seconds, not for the build. Files are written next to the old ones and renamed into place, so a running `upgrade.sh` keeps reading its own old copy. Before the files change, the database is backed up encrypted (`database.smpbackup`, `SMP_BACKUP_KEY`; details in [security.md](security.md)); `upgrade.sh --rollback` puts the files back, builds the old version while the current one runs, then stops the server and restores the database. A failed upgrade does the same by itself, importing the database only if the new version had already migrated it (`db-revision` in the backup vs. `alembic_version`). After a successful upgrade only the newest backup is kept.

**nginx** (`nginx/`, an image built on `nginx:1.30.7-alpine`) is the only web entry point:

- **Routing by name:** `SMP_SERVER_NAMES` → the portal (HTTP redirects to HTTPS); any other name → the DNS sinkhole's block page (port 80, and 443 after the browser's certificate warning). A name that is neither SMP's nor blocked gets a short "unknown address" page.
- **TLS:** on start the container creates a self-signed certificate for exactly `SMP_SERVER_NAMES` (renewed when the names change) unless `nginx/certs/` holds a certificate someone else issued, which is never touched. No HSTS while the certificate is self-signed, or browsers wouldn't let users past the warning.
- **Hardening:** login and general rate limits, `nosniff`, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `Referrer-Policy`, 64 MB upload limit (imports).
- **Client IPs:** nginx overwrites `X-Forwarded-For` with the real client address; the server trusts it only from SMP's docker network (`SMP_DOCKER_SUBNET`, fixed so it can be trusted), so audit IPs and the login throttle see real clients and nobody can fake theirs.
- The server (8000) and the block page/sync API (8080) are not published; only nginx (80/443) and DNS (port 53 on the LAN address `SMP_SINKHOLE_IP`; CoreDNS listens on 1053 inside its container) are. The DNS container keeps the last synced lists on the `dnsdata` volume.

## Not done yet (intentionally)

- Agent: key in the TPM, automatic DNS setting on install; an MSI/installer around `smp-agent install`; software packages that are more than one file (a folder with setup.exe), MSIX/AppX, per-user (HKCU) installs
- Monitoring with several server processes (viewer/frame state is per process)
- Per-org overrides of role → permission mappings (defaults come from manifests)
- Roles granted via groups (e.g. everyone in "Staff" is a teacher). Planned with directory sync.
- Audit retention job (setting `core.audit_retention_days` exists)
- OIDC login, MFA
