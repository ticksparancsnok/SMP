# Security notes

How SMP's core protects accounts, what operators must configure, and the decisions taken on the security reviews and releases. Read with [architecture.md](architecture.md).

## Operator checklist

1. **`SMP_SECRET_KEY`**: required, at least 32 random characters. The server refuses to start without it.
   - **Generate:** `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
   - **Where to keep it:** in a file passed as `SMP_SECRET_KEY_FILE` (e.g. a Docker secret, `chmod 600`) rather than in the environment. Never copy it to where you keep database backups: key + DB dump = readable unused one-time passwords. (The backups `upgrade.sh` makes are encrypted with their own key, `SMP_BACKUP_KEY`; see below.)
2. **TLS and the reverse proxy** come with the Docker stack: the bundled nginx terminates TLS, sets `SMP_COOKIE_SECURE=true`, forwards real client IPs (trusted only from SMP's docker network) and applies the rate limits below (`nginx/templates/smp.conf.template`). Replace the self-signed certificate with a real one in `nginx/certs/`; once you have, consider adding HSTS there.
3. **If you run your own proxy instead** (outside Docker): keep `SMP_COOKIE_SECURE=true`, set `SMP_FORWARDED_ALLOW_IPS` to the proxy's address (otherwise every client appears to come from the proxy and shares one login-throttle budget), and rate-limit requests there; the app throttles logins itself (below), but general request floods belong at the proxy:

   ```nginx
   limit_req_zone $binary_remote_addr zone=smp_login:10m rate=10r/m;
   limit_req_zone $binary_remote_addr zone=smp_all:10m   rate=20r/s;

   server {
       listen 443 ssl;
       # ssl_certificate ...;
       location = /api/v1/auth/login {
           limit_req zone=smp_login burst=5 nodelay;
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_set_header Host $host;
       }
       location / {
           limit_req zone=smp_all burst=40 nodelay;
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_set_header Host $host;
       }
   }
   ```
4. **`SMP_DNS_SYNC_TOKEN`**: required by Compose, ≥32 random characters, same value on the server and the DNS sinkhole (`DNS_SYNC_TOKEN`). The sinkhole's port 8080 serves the block page to every client, so its `/api/blocklist/*` endpoints only accept this Bearer token; with no token set they are disabled.
5. **Local development over plain http:** set `SMP_COOKIE_SECURE=false`, otherwise the browser drops the session cookie and you can't stay logged in. Never use it in production.

6. **`SMP_BACKUP_KEY`**: made by `install.sh`, encrypts the database backups `upgrade.sh` takes before every upgrade (see below). Keep a copy of `.env`, or at least this key, **away from the server** (a password manager): the backups can't be read without it, and a backup that leaks on its own is useless.

### Database backups (`upgrade.sh`)
- **When:** before every upgrade, `upgrade.sh` stores `backups/<time>-from-<version>/database.smpbackup` (a full `pg_dump`, plus the files it replaces) and reads it back to check it before it changes anything. `./upgrade.sh --rollback` restores files and database from it.
- **Encryption** (`smp/core/backup.py`, `tools/db-backup.sh`): streamed AES-256-GCM in 64 KiB chunks (the STREAM construction, as in `age`), key from `SMP_BACKUP_KEY` via HKDF with a fresh salt per backup. Changed, reordered, cut-off or extended files are refused, and a wrong key is reported as such. The plain dump never touches the disk: `pg_dump` in the db container is piped into `smp backup encrypt` in a one-off server container, which is also the only container that gets the key.
- **Why its own key:** not `SMP_SECRET_KEY`, because the dump contains the encrypted one-time passwords that key opens. Not the database password, because changing it would lock every old backup, and it's in the database container's environment anyway.
- **What it protects, and what not:** a backup copied off the server (USB stick, NAS, cloud folder) without the key. It does **not** protect against someone with access to the server's SMP folder: `.env` is next to `backups/`, and they could read the live database anyway. Backups are `chmod 600` in a `700` folder.
- **Restoring** goes into a new database first, which replaces the current one only once it fully worked. The replaced database is kept as `smp_before_restore` until the next restore.
- **Failed upgrades go back by themselves:** the backup folder records the running version (`installed-version`) and the database's migration revision (`db-revision`). If the new version doesn't build or start, `upgrade.sh` puts the old files back, starts the old version, and imports the database backup if the new version had already migrated the database (otherwise the database is kept, with nothing lost).
- **Only the newest upgrade backup is kept:** a successful upgrade removes the older `backups/*-from-*` folders (a rollback only goes one upgrade back). Other folders in `backups/` are left alone.
- TODO(scheduled-backups, issue #13): there are no regular backups between upgrades yet (scheduling, retention, a copy off the server).

### Rotating `SMP_SECRET_KEY`
1. Set the new key as `SMP_SECRET_KEY` and the old one as `SMP_SECRET_KEY_PREVIOUS`, then restart. Both keys decrypt, and new data uses the new key.
2. Run `smp reencrypt-otps`. It re-encrypts every stored one-time password with the new key.
3. Remove `SMP_SECRET_KEY_PREVIOUS` and restart.

If a key is lost or leaked, rotate it and issue new one-time passwords to accounts that aren't activated yet. Logins are unaffected either way: they check the argon2 hash, not the encrypted copy.

## How accounts are protected
- **Passwords:**
  - **Hashing:** argon2id (64 MiB, t=3) in worker threads, at most `SMP_HASH_CONCURRENCY` at once (default: half the CPUs), so a login flood can't stall the server or exhaust RAM.
  - **Policy (`smp/core/passwords.py`):** at least 8 characters, letters and digits, at least 5 distinct characters, not a common password, not containing the username, no leading or trailing spaces.
- **One-time passwords:**
  - **Generation:** individual (never shared), random ~59 bits, valid for `core.otp_valid_days`.
  - **Storage:** argon2 hash (16 MiB) plus a Fernet-encrypted copy for login slips.
  - **Use:** the first login forces a password change, which clears the OTP and rotates the session.
- **Login throttling (`smp/core/throttle.py`):**
  - **Limits:** 5 failed logins for one account (from any IP, within 15 min) lock it for 60 s; plus all attempts per IP per minute.
  - **No enumeration:** unknown usernames are locked exactly like real ones.
  - **Known trade-off:** anyone can keep an account (e.g. the admin's) locked by failing 5 times a minute. The short lockout keeps that a nuisance; longer lockouts are marked `TODO(evaluate)` in `throttle.py`.
  - **Restarts:** the state is rebuilt from the audit log at startup.
- **Sessions:** random 256-bit tokens; only their SHA-256 is stored. Cookies are HttpOnly and `SameSite=Strict`, with an Origin check on state-changing requests. A password change ends all sessions.
- **Audit:** append-only (enforced by a database trigger). Logins, failures, resets and exports are all logged. Secrets (passwords, OTPs) never appear in it. If a failed-login audit row can't be written, it goes to the server log instead.

## How the PC agents are protected
- **Identity:** each PC's own ECDSA P-256 key, made on install. No shared secret or enrollment code exists that could leak; the server stores only public keys.
- **Joining needs a human:** a join request does nothing until an admin approves it after comparing the fingerprint shown on the PC. Pending requests expire after 7 days and are capped (20 per network: one IPv4 address, or an IPv6 /64; 500 in total), and a machine (MAC) has one pending request at a time. A rejected machine is remembered by key and MAC for 90 days after it last asked ("allow again" in Devices undoes it), so it doesn't come back after an IP change or a reinstall. A key that already belongs to a device can't be approved for another one, and taking over a device whose own agent is online needs an explicit confirmation (`replace_online`), since a request with an online PC's hostname and MAC is easy to fake.
- **Every request is signed** (method, path, time, body hash); requests more than 5 minutes off are refused, and a signature is accepted only once. Unknown, rejected and reset keys all get the same 401.
- **The server's certificate:** OS-trusted, or the one pinned on first contact. A machine pretending to be SMP later (DNS/ARP spoofing) is refused. Trust on first use means the first contact happens at install time, when an admin is at the PC.
- **On the PC:** the key file is readable only by SYSTEM and administrators and encrypted with DPAPI for that Windows installation (a copied file or disk image doesn't work elsewhere). Its folder is `C:\ProgramData\SMP` only if that's SMP's own (owned by SYSTEM/Administrators, not a link); anyone can create folders in ProgramData, so one a student made beforehand is skipped for `SMP-1`, `SMP-2`, ... (created with owner and permissions set in one step; `platform/state_dir.rs`), and after 10 taken names `C:\Program Files\SMP\data`. The exe lives in `C:\Program Files\SMP` (admin-only), because the service runs it as SYSTEM. The screen helper runs as SYSTEM too, so students can't stop it or feed it fake screens; its pipe accepts only SYSTEM/admins and only the process the service started.
- **Monitoring privacy:** screens are sent only while a teacher with `classroom.screens.view` for that room has the page open (20 s keep-alive), full-size only for the PC they enlarged; frames are kept in memory only (latest per PC), dropped when nobody watches, never stored. Who watched which room, whom they enlarged, and when they stopped are audited. The tray icon on the PC is always visible and turns colourful while the screen is viewed. A `classroom` policy with `enabled: false` (e.g. outside lesson times) turns monitoring off for a room.
- **Screen lock (v1.0.4):** teachers with `classroom.screens.lock` for a room can cover its PCs' screens with a message and the school's logo and block keyboard and mouse; every lock and unlock is audited. It's a classroom tool, not a security boundary: Ctrl+Alt+Del always works, and the lock ends whenever the agent stops (fail-open). Details below.
- **Automatic updates:** an agent only installs what its own SMP server offers in the heartbeat config, downloaded from that server over the same (pinned or OS-trusted) TLS connection, after checking size and SHA-256 against the offer and that the file runs and reports the offered version. The file lands in `C:\Program Files\SMP` (administrators only). What this means: **whoever controls the SMP server can run code on every PC as SYSTEM**, so the server is as sensitive as the PCs' administrator accounts (it always could change what they do; now also what they are). There is no separate code signing yet: the exe is built on each school's server, so a signing key would have to live there too. Schools that want to roll out agent versions themselves switch automatic updates off (Settings -> Device agents) and install by hand.
- **Known limits:** an administrator of the PC can read the key (they own the machine anyway; a TPM-held key is planned). One server process holds viewer state.

## Review decisions

### v1.0.4: screen lock
Teachers lock a room's PCs (all or some) from the Monitoring page; the agent covers every monitor and blocks keyboard and mouse (`agent/smp-agent/src/capabilities/lock/`, `platform/win/lock_window.rs`, `platform/win/input_block.rs`).

| Change | Decision |
|---|---|
| Teachers can cover the screens and block the input of a room's PCs | **Room-scoped and audited:** `classroom.screens.lock` (teachers by default) works only in the rooms it was granted for; PCs named in a request must be in that room. Every lock (PCs and message) and unlock is in the audit log. A lock applies only while the PC is in the room it was locked in, so a laptop that leaves isn't stuck in someone else's lock. |
| Students getting out of a lock | **Accepted, not a security boundary:** low-level keyboard and mouse hooks swallow every key and mouse event (also Win, Alt+Tab, Alt+F4), the full-screen window covers every monitor, puts itself on top again every 0.5 s and hides the pointer, and it runs in the SYSTEM session helper, which students can't end. What Windows doesn't let any program block: **Ctrl+Alt+Del**. From its screen a student can sign out, switch user, lock the PC or shut down, which ends their session or returns to the lock; Task Manager opens behind the lock with no mouse to use it. Touch-screen edge swipes may still open Windows panels. Limiting Ctrl+Alt+Del without a kernel driver (Windows Keyboard Filter on Education/Enterprise, Ctrl+Alt+Del options while locked): issue #19. |
| A room that stays locked when SMP is away | **Fail-open:** the lock ends when the agent stops (service stopped, crash, uninstall) and when the watchdog restarts it after 15 minutes without the server; a restarted agent starts unlocked and locks again only once the server says so. So pulling the network cable for 15 minutes also unlocks a PC: fine for a classroom tool, and no outage can leave a school's PCs unusable. Locks are stored on the server, so a server restart doesn't unlock a room. |
| Text and a picture from SMP shown on student screens | **Limited:** the message is at most 500 characters (the agent removes control characters); the logo is set by admins only (`core.settings.write`), PNG only, at most 1 MB and 2048×2048, checked on upload. Locked PCs fetch it with a signed request and check it against the SHA-256 in their config; the agent decodes it with a pure-Rust PNG decoder under a 64 MB memory limit, and a logo it can't read is replaced by SMP's. |
| Agents learned the server's clock from long-poll answers (found while testing the lock) | **Fixed:** uvicorn dates an answer when the request arrived, so a heartbeat held for 25 s made the agent set its clock 25 s back and sign the next, identical heartbeat with the same time and signature. The server refused it as a replay, and the agent re-joined every half minute (which would also flash a lock off and on). nginx replaces the Date header, which hid it; a reverse proxy that passes it through would not. Agents now learn the time only from answers that arrive within 3 seconds (`core/api.rs`, `clock_offset`). |

### v1.0.4: software deployment
Decisions taken while building `server/smp/modules/software/` and the agent's `capabilities/software/`.

| Change | Decision |
|---|---|
| Admins can make every managed PC run an installer as SYSTEM | **Accepted, by design, admins only:** uploading/changing packages (`software.packages.write`) and assigning them (`software.assignments.write`) have no default role besides admin. Deliberately **not** a policy kind: room-scoped teachers may attach any existing policy to their room (`core.policies.assign`), which would have let them roll out software. Every upload, package change (with old and new SHA-256) and assignment is audited. Same trust model as agent updates (issue #17): whoever controls the SMP server can run code on the PCs. |
| Installers can contain license keys or licensed software | **Not public:** unlike `/downloads/smp-agent.exe`, a package file is served only to a signed agent whose computer the package is assigned to (`GET /software/agent/packages/{id}`); browser sessions and unknown or unassigned PCs get 401/404. |
| Uploads of several GB through the portal | **Streamed** to `SMP_SOFTWARE_DIR`, hashed on the way, never held in memory; refused early when the first bytes aren't an MSI/EXE or the declared size is over `SMP_SOFTWARE_MAX_UPLOAD_BYTES` (8 GiB), or when less than 1 GB of disk would stay free (a full disk would stop the database too). The upload releases its database connection while the body arrives. nginx lifts its 64 MB limit for this one admin-only endpoint only. |
| 30 PCs downloading a 500 MB installer on a 100 Mbps network | **Limited:** at most `max_parallel_downloads` (default 5) downloads at once; the rest get `503` with a randomized `Retry-After`. Downloads resume (`Range`) instead of starting over. Agents also wait a random `spread_s` before starting. |
| Command lines and detection rules come from admins and are run/read as SYSTEM | **Checked for shape, not for intent:** one line, no control characters, length limits; detection only under HKLM (no per-user hive as SYSTEM) or an absolute path / `%VARIABLE%\...`. The agent must run installers without a shell (no `cmd /c`), so arguments can't chain commands beyond what the installer itself does. |
| Deleting an assignment, or a package with assignments | **Leaves programs installed / refused:** only an explicit `remove` assignment uninstalls; a package can't be deleted while assigned (computers would keep a program SMP can neither update nor remove). |
| Agents report their own install results | **Accepted:** reports are signed by the device, count only for that device and its own school's packages, and are shown as the agent's word (state, exit code, a 500-character message with control characters removed). |
| The agent runs what the server's config describes, as SYSTEM | **Checked again on the PC, then trusted:** a package is skipped unless its download path is SMP's software endpoint (no other host), its SHA-256 and size look right, its id has only hex digits and dashes (it becomes part of file names) and its command lines are single lines; the file must match the announced size and SHA-256 before it runs. Installers are stored in the agent's own folder (SYSTEM/administrators only) and run with `CreateProcess` (no `cmd.exe`): the admin's arguments reach the installer verbatim but can't start other programs. Beyond that the agent trusts its server, as for agent updates (issue #17). |
| An installer that waits for a click in session 0 | **Stopped after the package's timeout** (default 60 minutes) and reported as failed, so one bad package can't block the others forever. A running installation isn't killed when the agent stops (a half-done installation is worse); the next start checks the detection rule. |
| Restarting students' computers | **Only if the package says so** (`always`, or `if_required` and the installer asked), at most once per boot, with Windows' own countdown warning and a message naming the programs (`reboot_warning_s`, 30 s when nobody is logged on). `never` (the default) only reports `reboot_pending`. |
| A failing installer retried every half hour would re-download hundreds of MB each time | **Backs off:** 1, 2, 4, ... 24 hours, keeping the downloaded file meanwhile; a changed package (file, arguments, rules) is tried at once. |

### v1.0.3: installing, upgrading, backups, agent updates
Security-relevant changes of this release and the decisions behind them.

| Change | Decision |
|---|---|
| `install.sh` / `upgrade.sh` download SMP from GitHub (or `--download-url`) | **Accepted:** HTTPS to GitHub; `--sha256` pins an expected download. Releases aren't signed yet. A release can never write `.env`, `nginx/certs/` or `backups/` (`tools/release.sh`), and its paths are checked (no `..`, no symbolic links). |
| Upgrades back up the whole database next to `.env` | **Encrypted** with its own `SMP_BACKUP_KEY` (streamed AES-256-GCM, see "Database backups"); `chmod 600` in a `700` folder; only the newest upgrade backup is kept. Protects backups copied off the server, not the server itself. |
| A failed upgrade could leave the school half on the new version | **Fixed:** it goes back by itself (files, and the database if the new version had already migrated it); `./upgrade.sh --rollback` goes back later. The certificate the PCs pinned is never touched, and `upgrade.sh` warns if it changed anyway. |
| PC agents now install the agent their SMP server offers | **Accepted, by design:** whoever controls the SMP server can run code on the PCs as SYSTEM. Size, SHA-256 and a `--version` run are checked before installing; a version that doesn't come up is undone (Windows). Schools can switch it off (Settings -> Device agents). Limiting this further (admin approval per version, staged rollout, signed builds): issue #17. |
| Agents could stay disconnected after the server was away (issue #10) | **Fixed:** a heartbeat without an answer after 60 s counts as an error, network errors reset the connection pool, retries back off to at most 30 s, and a watchdog restarts the agent after 15 minutes without contact. |
| The offered agent build was cached by file size + modification time, which copies can keep | **Fixed:** the cache also uses the inode and change time, and the `.version` file. |
| Agents report their own IP address (devices behind a NAT showed the router's) | **Accepted:** the reported address comes in the agent's signed heartbeat, so only an enrolled agent (SYSTEM on its PC) can set it, and only for its own device; it decides the device's displayed IP and, for "by network" devices, its room (so its policies). Nonsense (unspecified, multicast, not an address) is ignored. The address SMP sees is kept next to it (`observed_ip`) and is what the per-address limit on join requests uses. |
| Hosts imports keep their whole domain list in memory until confirmed; staff could hold 5 × 50 MB per school | **Fixed:** one import per person (a new upload replaces their unconfirmed one; not while it's being checked or saved) and at most 3 per school holding domains; finished and failed imports free theirs. |
| A failed file copy during an upgrade went unnoticed (`tools/release.sh`), and an unreadable file looked absent | **Fixed:** every copy, backup and removal is checked, so the automatic rollback starts; an unreadable file stops the upgrade before anything changes. |

### v1.0.3: attack run (students' side)
Each finding was reproduced first (in-process against a test database, against a real nginx, or worked out from the Windows APIs where no Windows machine was at hand).

| Finding | Decision |
|---|---|
| A student could create `C:\ProgramData\SMP` before the agent was installed and stay its owner: re-grant themselves access, read the device key (machine-scope DPAPI doesn't stop local users), send the teacher fake screens; and the SYSTEM service would write into a folder they control | **Fixed:** the agent uses that folder only if it's SMP's own (owned by SYSTEM/Administrators, not a junction/link), else `SMP-1`, `SMP-2`, ..., created with owner and permissions in one step; the same rule finds the same folder at every start and update. A PC that already used a squatted folder needs `smp-agent install` again (new key; approve it as the same device). |
| Guessing the current password on the change-password form had no limit (an unlocked teacher PC) | **Fixed:** the same per-account counter as the login, for everyone: 5 wrong current passwords lock the account (login too) and end its sessions; audited and rebuilt from the audit log after a restart. |
| A join request with an online lab PC's hostname and MAC was suggested and preselected as that PC; approving it cut the real PC off and let the laptop be it | **Fixed:** not preselected when that PC's agent is online, a warning in the dialog, and the server refuses unless `replace_online` is confirmed (audited). |
| One laptop could fill the 500 pending join requests from its IPv6 addresses (each counted alone), keeping real PCs out for 7 days; a rejected agent was back with a new request within seconds | **Fixed:** the per-sender limit counts an IPv6 /64 as one sender; one pending request per MAC; rejections are remembered by key and MAC until "allow again". |
| The public agent download (~4 MB) only had the general limit: one PC could pull ~600 Mbit/s and fill a school network | **Fixed:** its own nginx limit (10 per minute per address, burst 10, 4 at a time): about 5 Mbit/s per address. |
| The DNS sinkhole's sync API was reachable from any client through a blocked site's name (token-protected) | **Fixed:** nginx answers 404 for `/api/blocklist/` there; SMP's server reaches the sinkhole directly on the docker network. |
| Monitoring sees only the primary display, and windows that exclude themselves from capture | **Known limits:** candidates for later (all displays or a display count; reporting windows hidden from capture). |

### Third review: attack run (v1.0.1)
Each finding was first reproduced with a working exploit against a test database.

| Finding | Decision |
|---|---|
| API tokens outlived the person who created them (a deleted admin's token kept working), and a token could mint child tokens that survived revoking the parent | **Fixed:** deleting a user or removing their admin role deletes their tokens and, transitively, tokens created by those tokens; audited as `core.token.delete_with_user` / `core.token.delete_with_admin_role` (id, name, permissions only). Disabling an admin without demoting them leaves their tokens working (a disabled admin can be re-enabled; demote to cut off their tokens). |
| No per-account limit on password guessing: rotating source IPs (an IPv6 /64) reset the per-IP+username counter | **Fixed:** per-account lockout, 5 failures → 60 s, from any IP (see above). |
| Students could create short links with any code on the school's own domain (phishing) | **Fixed:** students no longer have `shortlinks.links.create`; migration 0007 disables links students already made (disabled, not deleted). |
| Any teacher could block a whole TLD (`com` with subdomains) and cut the school off; domain validation was minimal | **Fixed:** allowlist checked before the blocklist; public suffixes can be neither blocked nor allowed; strict domain validation. |
| DNS hosts import has no size limit | **Accepted:** authenticated (teachers); parsing is now strict and the import does one query instead of one per domain. |
| Login time reveals accounts that still have their one-time password (cheap OTP hash vs. expensive dummy hash) | **Deferred:** `TODO(evaluate)` in `security.py`; schools run SMP behind NAT. |
| The DNS resolver answers anyone who can reach port 1053 (open resolver) | **Deferred:** `TODO(evaluate)` in the Corefile and `docker-compose.yml`; behind NAT it isn't reachable from outside. |
| `/api/docs` and `/api/openapi.json` were public | **Fixed:** admins (signed-in users with the admin role) only; API tokens are refused. |

### v1.0.1
| Finding | Decision |
|---|---|
| The DNS sinkhole's `/api/blocklist/import` and `/entries` accepted writes from anyone who could reach port 8080 (every client, since it also serves the block page), trusting a client-sent `X-Username`; `/sync` was open when no token was set | **Fixed:** every `/api/blocklist/*` endpoint requires `Authorization: Bearer <DNS_SYNC_TOKEN>` (constant-time compare); with no token configured the API answers 503 instead of being open; Compose requires the token. The block page stays public. Covered by `schoolfilter_test.go`. |
| Slow clients could hold block-page connections open | **Fixed:** `ReadHeaderTimeout` of 10 s. |

### Second review (v1.0.0)
| Finding | Decision |
|---|---|
| Leaked `SMP_SECRET_KEY` lets an attacker "forge OTPs" | Partly. Login verifies the argon2 hash, so a key alone forges nothing. Key + DB dump does expose *unused* OTPs, so: `SMP_SECRET_KEY_FILE`, key rotation (`SMP_SECRET_KEY_PREVIOUS`, `smp reencrypt-otps`) and the guidance above. |
| Restart clears login throttling | **Fixed:** re-seeded from the audit log on startup (no Redis needed for a single process). |
| 429 vs 401 enables username enumeration | **Not an issue:** the throttle applies identically to existing and unknown usernames (`test_throttle_is_per_ip_and_username_and_reveals_nothing`). 429 stays so users know to wait. |
| OTP hash memory too low (DoS) | Raised 8 → 16 MiB as suggested. Note: a cheaper hash means *less* DoS potential, and OTP guessing is throttled and infeasible anyway. |
| Hash semaphore not tunable | **Fixed:** `SMP_HASH_CONCURRENCY`. |
| Key length check allows 31 chars | **Not an issue:** `< 32` already rejected 31. Added a placeholder check (too few distinct characters). |
| Decrypt failure "leaks" key state | Only admins/class teachers see the export. **Changed:** failures are now logged instead of silent. |
| Failed-login audit lost if commit fails | **Fixed:** falls back to the server log and still answers 401. |
| Room-scoped permissions could be granted globally | **Not an issue:** roles are fixed (admin/teacher/student). Admin can't be room-scoped; a teacher assigned school-wide is a deliberate choice. |
| Strip whitespace from passwords | **Rejected:** silently altering a secret breaks logins. Surrounding whitespace is now refused with a clear message instead. |
| Duplicate e-mails | **Fixed:** unique per school, case-insensitive (DB index + friendly 409). |
| Per-IP limit generous / no global limit | Lowered to 20 attempts/min/IP. Global request limiting belongs at the proxy (config above): an app-wide login cap would let an attacker lock *everyone* out. |
| Secret key only checked when first used | **Fixed:** the server refuses to start. |
| "No tests for OTP lifecycle / throttling" | They existed (`tests/test_auth.py`). Added more in `tests/test_security_v1.py`. |

### First review
See git history (`Security hardening: per-user one-time passwords…`): shared default password → individual OTPs; lockout → throttle; argon2 off the event loop; streamed upload limit; password policy; session rotation; no credential-less accounts.
