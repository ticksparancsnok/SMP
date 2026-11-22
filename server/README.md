# SMP server (control plane)

Python 3.12+, FastAPI, SQLAlchemy 2 (async), Alembic, Postgres 15+.

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
    context.py      Ctx, Principal, require(): what every handler uses
    module.py       Module contract + registry
    policy.py       schedule evaluation, policy resolution, IP -> device/room
    events.py       event bus (NATS-style subjects)
    identity.py     identity provider interface (local now; ldap/google later)
    api/            core REST endpoints
  modules/
    __init__.py     the list of compiled-in modules
    shortlinks/     reference module
  migrations/       Alembic (one history for all modules)
  cli.py            smp preflight | status | migrate | bootstrap | create-token | reencrypt-otps | openapi | serve
```

See `../docs/architecture.md` and `../docs/writing-a-module.md`.

## DNS sinkhole module

The `dns` module provides control-plane blocklist management under:

```text
GET    /api/v1/dns/blocklist
POST   /api/v1/dns/blocklist
DELETE /api/v1/dns/blocklist/{entry_id}
POST   /api/v1/dns/blocklist/import-preview
POST   /api/v1/dns/blocklist/import
GET    /api/v1/dns/allowlist
POST   /api/v1/dns/allowlist
DELETE /api/v1/dns/allowlist/{entry_id}
GET    /api/v1/dns/check?domain=…        would this be blocked now, and by what
GET    /api/v1/dns/status
```

Manual entries set `addedBy` server-side from the authenticated principal. Hosts
imports set `addedBy` to `imported by: <username>`. Clients cannot send or
override `addedBy`.

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
| `SMP_DATABASE_URL` | `postgresql+asyncpg://smp:smp@localhost:5432/smp` | |
| `SMP_DEFAULT_ORG` | `default` | org slug used when login names none |
| `SMP_COOKIE_SECURE` | `true` | set `false` only for http dev |
| `SMP_SESSION_TTL_HOURS` | `12` | |
| `SMP_SECRET_KEY` / `SMP_SECRET_KEY_FILE` | none | **required** (the server won't start without it): ≥32 random chars; prefer the file variant. See `docs/security.md` |
| `SMP_SECRET_KEY_PREVIOUS` | none | old key(s) during a rotation; then run `smp reencrypt-otps` |
| `SMP_LOGIN_MAX_FAILURES` / `SMP_LOGIN_LOCKOUT_SECONDS` | `5` / `60` | failed logins for one account (any IP) before it's locked, and for how long |
| `SMP_LOGIN_IP_ATTEMPTS_PER_MINUTE` | `20` | all login attempts from one IP |
| `SMP_FORWARDED_ALLOW_IPS` | `127.0.0.1` | reverse proxies trusted for the client IP (IPs or CIDRs; must include yours). Compose sets it to SMP's docker network, where the bundled nginx runs |
| `SMP_HASH_CONCURRENCY` | half the CPUs | argon2 hashes running at once (64 MiB each) |
| `SMP_ENABLED_MODULES` | all | comma separated; core is always on |
| `SMP_CORS_ORIGINS` | none | only if the UI is served from another origin |
| `SMP_UI_DIR` | none | serve the built web UI (`WEBUI/dist`) at `/`; set in the Docker image |
| `SMP_DNS_SINKHOLE_URL` | `http://127.0.0.1:8080` | Go DNS sinkhole; `http://dns-sinkhole:8080` inside Compose |
| `SMP_DNS_SYNC_TOKEN` | none | **required for DNS sync**: shared secret, same value as the sinkhole's `DNS_SYNC_TOKEN` |
