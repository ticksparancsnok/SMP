# SMP – School Management Portal

One platform for managing school computers: DNS-based site blocking with automatic classification, user directory, classroom screen monitoring/locking, software deployment for Windows 10/11, and a link shortener.

- `server/`: control plane (Python, FastAPI): core + modules. See [server/README.md](server/README.md)
- `WEBUI/`: web interface (Vue 3 + TypeScript). See [WEBUI/readme.md](WEBUI/readme.md)
- `agent/`: the program on the school's Windows 10/11 computers (Rust): screen monitoring, later lock and software. See [agent/README.md](agent/README.md)
- `docs/architecture.md`: design decisions, domain model, policy resolution, permissions
- `docs/writing-a-module.md`: how to add a service module (backend + UI page)
- `docs/security.md`: operator checklist (secret key, reverse proxy, rate limits) and security review decisions

Quick start (needs Docker with the compose plugin):

```sh
./install.sh
```

The installer creates `.env` with random secrets, works out SMP's addresses (it
asks for extra names such as `smp.iskola.hu`), builds and starts the containers,
waits until everything is up, and on first setup asks for the school name and the
administrator's username. It ends by printing the administrator's **one-time
password**: sign in at https://localhost (or the LAN address it lists) with it, and
SMP asks you to choose your own. Run `./install.sh` again after updating SMP: it rebuilds, migrates and leaves
your data and admin alone. Keep a backup of `.env`; without it the database can't be
opened.

If the server doesn't come up, `docker compose logs server` starts with a plain
explanation (e.g. a database password that no longer matches the existing database).

API docs: https://localhost/api/docs (administrators only).

All web traffic goes through **nginx** (ports 80 and 443; change them with
`SMP_HTTP_PORT` / `SMP_HTTPS_PORT` in `.env`):

- requests for SMP's own names (`SMP_SERVER_NAMES`) get the portal, always over HTTPS;
- any other name is a site the DNS filter blocked, and gets the block page.

HTTPS starts with a self-signed certificate (browsers warn once). For a real one, put
`fullchain.pem` and `privkey.pem` into `nginx/certs/` and run
`docker compose restart nginx`; SMP never overwrites a certificate you provide.

The stack also runs the Go DNS sinkhole on **port 53 (UDP/TCP) of this server's LAN
address** (`SMP_SINKHOLE_IP`): give that address to the school's computers as their
only DNS server, ideally through the school's DHCP (DNS server option) rather than on
each PC, and don't add a public DNS as a secondary one (clients would use it to get
around the filter). Give the server a fixed address. Windows can't use a DNS server on
any other port; on a test machine whose port 53 is taken, `SMP_DNS_PORT=1053` in `.env`.

Blocked names resolve to the same address, where nginx shows the block page. Blocklist
changes made in the web UI or API are stored in PostgreSQL and synchronized to the
sinkhole, which keeps the last synced lists on a volume and keeps filtering while the
server is down; `./install.sh` re-syncs on every run (`smp dns-sync`). Only nginx and
the DNS port are published.

**Computers (agent).** `./install.sh` also builds the Windows agent, and SMP offers it at
`https://<smp address>/downloads/smp-agent.exe` (and under **Devices → Agent for
Windows**, with its checksum): everything stays on your own server. On each PC run, as
administrator, `smp-agent.exe install` (it asks for SMP's address). The
PC then shows up in **Devices → Waiting for approval** with a fingerprint; approve it
if the fingerprint matches what the PC printed, and put it in a room (fixed, or
"by network" for laptops that move between rooms/VLANs). Teachers then see the room's
screens on the **Monitoring** page, while the page is open; the PCs show an SMP tray
icon all the time, in colour while their screen is being viewed.
