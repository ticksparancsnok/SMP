# SMP – School Management Portal

One platform for managing school computers: DNS-based site blocking with automatic classification, user directory, classroom screen monitoring/locking, software deployment for Windows 10/11, and a link shortener.

- `server/`: control plane (Python, FastAPI): core + modules. See [server/README.md](server/README.md)
- `WEBUI/`: web interface (Vue 3 + TypeScript). See [WEBUI/readme.md](WEBUI/readme.md)
- `docs/architecture.md`: design decisions, domain model, policy resolution, permissions
- `docs/writing-a-module.md`: how to add a service module (backend + UI page)
- `docs/security.md`: operator checklist (secret key, reverse proxy, rate limits) and security review decisions

Quick start:

```sh
docker compose up -d --build
docker compose exec server smp bootstrap --org-name "My School"
# open http://localhost:8000 (API docs: /api/docs)
```

The Docker stack also starts the Go DNS sinkhole on UDP/TCP port 1053 and its
block page on port 8080. Mutations made in the web UI or control-plane API are
stored in PostgreSQL and synchronized to
`http://dns-sinkhole:8080/api/blocklist/sync`.
