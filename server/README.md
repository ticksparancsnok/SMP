# SMP server (control plane)

Python 3.15+, FastAPI, SQLAlchemy 2 (async), Alembic, Postgres 15+.

## Development setup

```sh
# from the repo root: start Postgres
docker compose up -d db

cd server
python -m venv .venv && . .venv/bin/activate      # fish: source .venv/bin/activate.fish
pip install -e ".[dev]"

export SMP_DATABASE_URL=postgresql+asyncpg://smp:smp@127.0.0.1:5432/smp
export SMP_COOKIE_SECURE=false                     # plain http in dev
export SMP_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")  # keep it stable
smp migrate
smp bootstrap --org-name "Teszt Iskola"            # asks for the admin password (--otp: one-time password instead)
smp serve --port 8000
```

Interactive API docs: http://127.0.0.1:8000/api/docs (sign in as an admin first; everyone else gets 401/403)

## Tests

```sh
pytest              # needs `docker compose up -d db`; override with SMP_TEST_DATABASE_URL
ruff check smp tests
```

The test database is dropped and recreated each run, and the migrations are applied to it, so the migrations get tested too.

## Layout

```
smp/
  core/
    models.py       shared domain model (read this first)
    roles.py        the role table: rank, where each role may be assigned, inheritance, who may grant what
    context.py      Ctx, Principal (org / building / room grants), require(): what every handler uses
    module.py       Module contract + registry
    policy.py       schedule evaluation, policy resolution, IP -> device/room
    events.py       event bus (NATS-style subjects)
    identity.py     identity provider interface (local now; ldap/google later)
    api/            core REST endpoints
  version.py        versions from ../Version.toml (the only place they're written)
  modules/
    __init__.py     the list of compiled-in modules
    shortlinks/     reference module
    dns/            DNS blocklist/allowlist, synced to the Go sinkhole
    agent/          PC agents: join requests, approval, signed heartbeats (long poll)
    classroom/      monitoring: who watches which room, screen frames (memory only), seating plans
    software/       software deployment: installer uploads, packages, assignments, PCs' install states
  migrations/       Alembic (one history for all modules)
  cli.py            smp preflight | status | migrate | bootstrap | dns-sync | create-token | reencrypt-otps | backup | openapi | serve
```

See `../docs/architecture.md` and `../docs/writing-a-module.md`.

## DNS sinkhole module

The `dns` module provides control-plane blocklist management under:

```text
GET    /api/v1/dns/blocklist?q=…&limit=…&offset=…   one page: {items, total}
POST   /api/v1/dns/blocklist
DELETE /api/v1/dns/blocklist/{entry_id}
POST   /api/v1/dns/blocklist/imports                 hosts file (raw body, max 50 MB) -> job, 202
GET    /api/v1/dns/blocklist/imports/{id}            state, progress, counts (poll it)
GET    /api/v1/dns/blocklist/imports/{id}/domains    ?kind=new|existing|skipped: preview, paged
POST   /api/v1/dns/blocklist/imports/{id}/confirm    add the new domains (in the background)
DELETE /api/v1/dns/blocklist/imports/{id}            forget it (not while saving)
GET    /api/v1/dns/allowlist
POST   /api/v1/dns/allowlist
DELETE /api/v1/dns/allowlist/{entry_id}
GET    /api/v1/dns/check?domain=…        would this be blocked now, and by what
GET    /api/v1/dns/status
```

Manual entries set `addedBy` server-side from the authenticated principal. Hosts
imports set `addedBy` to `imported by: <username>`. Clients cannot send or
override `addedBy`.

Imports are background jobs (`smp/modules/dns/imports.py`), so a big list (StevenBlack's
hosts file: 76k domains) never stalls the rest of SMP: checking runs in a worker thread,
saving COPYs the domains into a temporary table and adds them with one
`INSERT ... ON CONFLICT DO NOTHING`, and the job reports its progress for the UI. Jobs
live in the server process's memory and belong to the user who started them. With 76k
domains already listed, adding or deleting one entry still re-sends the whole list, but
Postgres builds that JSON itself, so it costs the event loop nothing.

After a successful database transaction, the API sends the complete blocklist to
the Go sinkhole:

```text
POST /api/blocklist/sync
Authorization: Bearer <SMP_DNS_SYNC_TOKEN>
```

The Go service stores the synchronized state in memory and writes
`blocklist.json`. DNS request handling does not access PostgreSQL or the control
plane hot path.

Configuration:

```text
SMP_DNS_SINKHOLE_URL=http://127.0.0.1:8080
SMP_DNS_SYNC_TOKEN=<shared secret>
```

In Docker Compose the sinkhole is available as `http://dns-sinkhole:8080`.
Set the same `SMP_DNS_SYNC_TOKEN` value in the server and
`DNS_SYNC_TOKEN` environment of the Go service (Compose does this from `.env`
and refuses to start without it). Every `/api/blocklist/*` endpoint of the Go
service requires this token; without one configured its API is disabled.
`GET /api/v1/dns/status` reports `sync_configured: true` only when the sinkhole
is reachable **and** accepts the token.

Apply the database migration with:

```sh
smp migrate
```

## Configuration (environment)

| Variable | Default | |
|---|---|---|
| `SMP_VERSION_FILE` | the nearest `Version.toml` above the package | where the versions are read from (`smp/version.py`); the Docker image sets `/app/Version.toml` |
| `SMP_BACKUP_KEY` / `SMP_BACKUP_KEY_FILE` | none | only for `smp backup` (database backups of `upgrade.sh`); see `docs/security.md` |
| `SMP_DATABASE_URL` | `postgresql+asyncpg://smp:smp@localhost:5432/smp` | |
| `SMP_DB_ECHO` | `false` | log every SQL statement (debugging) |
| `SMP_DEFAULT_ORG` | `default` | org slug used when login names none |
| `SMP_COOKIE_SECURE` | `true` | set `false` only for http dev |
| `SMP_SESSION_TTL_HOURS` | `12` | |
| `SMP_SESSION_COOKIE_NAME` | `smp_session` | |
| `SMP_SECRET_KEY` / `SMP_SECRET_KEY_FILE` | none | **required** (the server won't start without it): ≥32 random chars; prefer the file variant. See `docs/security.md` |
| `SMP_SECRET_KEY_PREVIOUS` | none | old key(s) during a rotation; then run `smp reencrypt-otps` |
| `SMP_LOGIN_MAX_FAILURES` / `SMP_LOGIN_LOCKOUT_SECONDS` | `5` / `60` | failed logins for one account (any IP) before it's locked, and for how long |
| `SMP_LOGIN_IP_ATTEMPTS_PER_MINUTE` | `20` | all login attempts from one IP |
| `SMP_FORWARDED_ALLOW_IPS` | `127.0.0.1` | reverse proxies trusted for the client IP (IPs or CIDRs; must include yours). Compose sets it to SMP's docker network, where the bundled nginx runs |
| `SMP_HASH_CONCURRENCY` | half the CPUs | argon2 hashes running at once (64 MiB each) |
| `SMP_ENABLED_MODULES` | all | comma separated; core is always on |
| `SMP_AGENT_LONGPOLL_SECONDS` | `25` | how long a PC's heartbeat waits for a config change before the server answers anyway |
| `SMP_AGENT_DOWNLOAD_DIR` | none | folder with `smp-agent.exe` (+ `smp-agent.version`) offered at `/downloads/smp-agent.exe`; the Docker image builds it and sets this |
| `SMP_SOFTWARE_DIR` | none | where uploaded installers are kept (the `softwaredata` volume in the Docker stack); unset = uploads are refused (503) |
| `SMP_SOFTWARE_MAX_UPLOAD_BYTES` | `8589934592` (8 GiB) | largest installer that can be uploaded; nginx passes that endpoint through without its own limit |
| `SMP_CORS_ORIGINS` | none | only if the UI is served from another origin |
| `SMP_UI_DIR` | none | serve the built web UI (`WEBUI/dist`) at `/`; set in the Docker image |
| `SMP_DNS_SINKHOLE_URL` | `http://127.0.0.1:8080` | Go DNS sinkhole; `http://dns-sinkhole:8080` inside Compose |
| `SMP_DNS_SYNC_TOKEN` | none | **required for DNS sync**: shared secret, same value as the sinkhole's `DNS_SYNC_TOKEN` |
