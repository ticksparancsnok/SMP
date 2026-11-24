# SMP – School Management Portal

One platform for managing school computers: DNS-based site blocking with automatic classification, user directory, classroom screen monitoring/locking, software deployment for Windows 10/11, and a link shortener.

- `server/`: control plane (Python, FastAPI): core + modules. See [server/README.md](server/README.md)
- `WEBUI/`: web interface (Vue 3 + TypeScript). See [WEBUI/readme.md](WEBUI/readme.md)
- `agent/`: the program on the school's Windows 10/11 computers (Rust): screen monitoring, screen lock, software installation. See [agent/README.md](agent/README.md)
- `docs/architecture.md`: design decisions, domain model, policy resolution, permissions
- `docs/writing-a-module.md`: how to add a service module (backend + UI page)
- `docs/security.md`: operator checklist (secret key, reverse proxy, rate limits) and security review decisions

Quick start (needs Docker with the compose plugin, and curl or wget):

```sh
mkdir -p /opt/smp && cd /opt/smp
curl -fsSLO https://raw.githubusercontent.com/ticksparancsnok/SMP/main/install.sh
bash install.sh
```

The installer downloads the newest SMP (the `main` branch on GitHub; `--ref` picks a
branch or tag, `--download-url` a modified SMP from your own server, `--help` lists
the options) into its folder, creates `.env` with random secrets, works out SMP's
addresses (it asks for extra names such as `smp.iskola.hu`), builds and starts the
containers, waits until everything is up, and on first setup asks for the school name
and the administrator's username. It ends by printing the administrator's **one-time
password**: sign in at https://localhost (or the LAN address it lists) with it, and
SMP asks you to choose your own. Keep a backup of `.env`; without it the database can't
be opened.

**Updating:** `./upgrade.sh` in the same folder. It downloads the newest version and, if
it's newer than the installed one (`[release]` in `Version.toml`), shows what changes,
backs up the database (encrypted with `SMP_BACKUP_KEY` from `.env`) and the files it
replaces into `backups/`, replaces the files and rebuilds. Your data, `.env` and the HTTPS
certificate are kept, and SMP keeps running while the new version builds.
If the new version doesn't build or start, it goes back to the previous one by itself;
`./upgrade.sh --rollback` goes back later, files and database. Only the newest backup is
kept. It compares `[release]`; a newer build of the same release (e.g. its components
went from `1.0.3 B` to `1.0.3 C`) is installed with `./upgrade.sh --force-components`,
which upgrades when any component's version differs (`./upgrade.sh` alone points this
out). `--force` reinstalls or downgrades whatever the versions say. Before changing
anything it checks that Docker can reach the package registries the rebuild downloads
from (PyPI, npm, Rust crates, Go modules); without internet access it stops there.
`./install.sh` again first asks GitHub whether a newer version exists. If one does, it
**stops without rebuilding** (exit status 3) and points to `./upgrade.sh`, so upgrading
stays a deliberate step; otherwise it checks the configuration and rebuilds what's
installed. In scripts (or without internet access) use `./install.sh --local`: it
rebuilds what's installed and never checks for or downloads anything. In a git
checkout (development) `./install.sh` uses the checkout's files as they are, and
`./upgrade.sh` refuses: update with git.

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
icon all the time, in colour while their screen is being viewed. After that first
install the agents update themselves to the version SMP offers (it comes with every SMP
update; **Settings → Device agents** can switch it off), and reconnect by themselves
when SMP was away. See [agent/README.md](agent/README.md).

**Software.** In **Software deployment**, admins upload a program's installer (`.msi` or
`.exe`, kept on the server's `softwaredata` volume) with its silent-install arguments,
how to recognise it as installed, how to remove it and whether to restart afterwards,
then assign it to the whole school, a room or one computer. The PCs install it by
themselves (a few download at once, so a 100 Mbps network isn't flooded) and SMP shows
each computer's state; teachers see their rooms'. Only a "remove" assignment uninstalls.

## License

SMP - School Management Portal, https://github.com/ticksparancsnok/SMP
Copyright (C) 2026 GG4Ever

SMP is free software under the **GNU Affero General Public License v3 or later**
([LICENSE](LICENSE)). Anyone may use, study, change and share it, also commercially.
Modified versions must stay under the same license and publish their source code,
also when they're only offered as a service over the network (e.g. hosting SMP for
schools).

One additional term ([NOTICE](NOTICE), AGPL section 7(b)): modified versions keep the
attribution "Based on SMP - School Management Portal" with the address above, in their
documentation and in the web interface (the small notice on the sign-in page and in
the side bar). Everything else, including the name of your system, is yours to change.

Third-party components and their licenses are listed in [NOTICE](NOTICE). Every source
file carries an `SPDX-License-Identifier` line. Contributions are accepted under the
same license.
