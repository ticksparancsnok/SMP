# SMP agent (Rust)

The program that runs on the school's computers. It asks the SMP server to manage
the computer (an admin approves it), keeps a heartbeat, and runs **capabilities**:
screen monitoring, the screen lock and software installation.

One `smp-agent.exe` (about 4 MB, no runtime or DLLs to install) does everything; what
it does depends on the command it's started with.

## Getting the exe

Every SMP server offers its own matching agent: `./install.sh` builds it into the
server's Docker image (`server/Dockerfile`, stage `agent`), and SMP serves it at

```
https://<smp address>/downloads/smp-agent.exe
```

No sign-in is needed, so you can download it right on the PC (the exe holds no
secrets; a PC does nothing until an admin approves it). In SMP, **Devices -> Agent for
Windows** shows the same link, the version, the SHA-256 checksum and the install
command with this server's address filled in. That's needed once per PC: after that the
agent updates itself (below), also when SMP is updated with `./upgrade.sh` (the upgrade
keeps the database with the PCs' approved keys and the HTTPS certificate they pinned).

## Installing on a PC (Windows 10/11)

As administrator (right click -> Run as administrator, or an admin command prompt):

```
smp-agent.exe install                       asks for SMP's address
smp-agent.exe install --server 192.168.1.10  or give it (https:// is added)
```

This copies the exe to `C:\Program Files\SMP`, creates the PC's key in
`C:\ProgramData\SMP`, sends the join request, prints a fingerprint such as
`DBF0-7132-FE3D-8381-6739`, and installs + starts the **SMP Agent** service. In SMP:
**Devices -> Waiting for approval**, compare the fingerprint, **Approve**, choose the room
(or "by network"). Within a few seconds the PC appears as online.

- Running `install` again with a new exe upgrades it (same identity, no new approval).
- `smp-agent.exe uninstall` removes the service; `--purge` also deletes the identity
  and logs (then remove the device in SMP too, or reset its identity).
- Logs: `C:\ProgramData\SMP\logs\agent.log` (service) and `helper.log` (screen/tray).
- `smp-agent.exe status` (as administrator) shows the server, fingerprint and device id.
- The folder is `C:\ProgramData\SMP` unless someone else had made a folder with that
  name before the install (anyone can create folders in ProgramData, and its creator
  owns it): then `SMP-1` (or the next free name), and `agent.log` says why. The agent
  only uses a folder owned by SYSTEM/Administrators, so it always finds its own again.
- A rejected computer isn't listed again (also not after an IP change or a reinstall):
  SMP -> Devices -> Rejected computers -> Allow again.

What the user sees: an SMP icon in the tray, always while the agent runs. **Grey** =
the agent runs, nobody views the screen; **colour** = a teacher is viewing it right
now. Hovering shows the same in words (Hungarian or English, by the Windows display
language). This is the GDPR notice; it can't be turned off, and it stays on the
taskbar instead of the hidden "^" area:

- **Windows 11:** the helper marks the icon "always shown" in the user's taskbar
  settings (`IsPromoted` under `HKCU\Control Panel\NotifyIconSettings`) within a few
  seconds of logon, and puts it back if someone hides it.
- **Windows 10:** the service sets the machine policy "Turn off notification area
  cleanup" (`NoAutoTrayNotify`), so the taskbar shows all tray icons, from the next
  logon. If the school's own policy already sets it, SMP leaves it alone; `uninstall`
  removes it only if SMP set it.

**Screen lock:** a teacher can lock the computers of their room (all or some) on the
Monitoring page. A locked computer shows the teacher's message under the school's logo
(Settings -> Monitoring; without one, the SMP logo) on every monitor, and keyboard and
mouse do nothing until the teacher unlocks it. With an empty message it says "This
computer was locked by the teacher" (Hungarian or English: the language of SMP's block
page, set in SMP's DNS settings, so a PC with English Windows in a Hungarian school
still shows Hungarian). What it doesn't stop, because Windows doesn't let any program: Ctrl+Alt+Del,
whose screen offers sign out, switch user, lock and shut down (all of which end the
session or come back to the lock). The lock goes away whenever the agent stops (service
stopped, uninstalled, or 15 minutes without the server: the watchdog restarts it
unlocked), so a server outage can never leave a room unusable; logging off and on again
doesn't unlock (the new session gets the lock too).

## Updates

Installed agents update themselves to the version their SMP server offers: `[agent]` in
SMP's `Version.toml`, built into the server image with every SMP update. Nobody has to
visit the PCs again after the first `install`.

- **When:** the heartbeat config tells the agent which version the server has for its
  platform (with size and SHA-256). If that differs from its own version (newer, or older
  after SMP itself went back a version: the server decides), the agent waits a random
  time up to *update_spread_s* (default 5 minutes, so a whole school doesn't download at
  once), then updates. Admins can switch updates off: **Settings -> Device agents**.
- **Checks before anything changes:** downloaded from the same server over the same
  pinned TLS connection; size and SHA-256 must match the offer; the new exe must start
  and answer `--version` with exactly the offered version.
- **Windows:** the running exe can't be overwritten, but it can be renamed. The agent
  moves itself to `smp-agent.old.exe`, puts the new exe in its place and starts a
  watcher (`smp-agent.old.exe update-watch`) outside the service, which restarts the
  service with the new exe (tray icon and helper included). The new version confirms
  once it reached the server; if it doesn't within 3 minutes, the watcher puts the old
  exe back, starts it again, and that version isn't tried for 6 hours (or until the
  server offers another one). `C:\ProgramData\SMP\logs\update.log` has the watcher's
  side. Each service start also re-applies the service settings (recovery actions), so
  a newer agent brings its own service configuration along.
- **Devices page:** shows each PC's agent version, with "-> version" while it differs
  from the offered one.
- **Linux** (development, test PCs): the agent replaces its binary and restarts itself in
  the same process, without a watcher.

## Staying connected

The agent retries every failed request (after 1, 2, 4, ... up to 30 s), gives up on a
heartbeat that doesn't answer within 60 s instead of waiting for it, drops pooled
connections after network errors (a restarted server leaves them dead), and looks up
the server's name at most every 30 s while it doesn't resolve (often SMP itself is the
school's DNS). On top of that, a watchdog restarts the agent if nothing reached the
server for 15 minutes (`SMP_AGENT_WATCHDOG_SECONDS` changes it, e.g. for tests); on
Windows the service manager starts it again 5 s later.

## Software

The PC installs and removes the programs SMP assigns to it (**Software deployment** in
SMP; admins upload the installers there). Everything runs as SYSTEM, without a window:

- **When:** shortly after the list changes (a random wait up to *spread_s*, so a room
  doesn't start at once), and every 30 minutes (a program someone uninstalled comes
  back; failed ones are retried). One package at a time.
- **How:** the package's detection rule says whether it's there (.msi: its product code;
  else a registry key under HKLM or a file). If something needs doing, the installer is
  downloaded from SMP (signed, continues where it stopped; SMP lets a few PCs download at
  once and tells the others to wait), checked (size, SHA-256), and run:
  `msiexec /i <file> /qn /norestart /l*v <log>` plus the package's arguments, or the
  .exe with its arguments. Exit code 0 is success, 3010/1641 success with a restart
  needed, 1618 "another installation is running" (tried again in 5 minutes). Then the
  rule is checked again and SMP gets the result.
- **Restart:** only if the package says so (*always*, or *if required* and the installer
  asked): Windows shows its own countdown with a message (*reboot_warning_s*, 30 s when
  nobody is logged on). With *never*, SMP shows "restart pending" until the PC restarts.
- **Failures** wait 1, 2, 4, ... up to 24 hours before the next try, unless an admin
  changes the package.
- **Removing:** only when SMP says *remove* (a deleted assignment leaves the program).
- Files: `C:\ProgramData\SMP\software\` (downloads, deleted after a successful
  installation; `logs\<package id>.log` is the Windows Installer log) and
  `software.json` (what SMP installed, product codes, failures, pending restarts).

## How it works

```
                      C:\Program Files\SMP\smp-agent.exe
 ┌───────────────────────────── session 0 ─────────────────────────────┐
 │ service (SYSTEM): join -> heartbeat (long poll) -> capabilities:     │
 │   screen (uploads JPEGs), lock, software (installers), updates       │
 └──────────────┬───────────────────────────────────────────────────────┘
                │ private named pipe (SYSTEM/admins only, checked process id)
 ┌──────────────┴──────────── user's session ──────────────────────────┐
 │ session-helper (SYSTEM, on the user's desktop): tray icon,           │
 │   screen capture of the primary display + shrink + JPEG,             │
 │   lock screen over every monitor + keyboard/mouse block              │
 └──────────────────────────────────────────────────────────────────────┘
```

- **Why a helper:** Windows services run in session 0, which has no screen. The
  service starts the helper in whoever's session is in front of the screen (the
  console, or a remote desktop session if nobody sits at the console) and replaces it
  when users log off/on. The helper runs as SYSTEM, not as the student, so a student
  can't kill it (to hide the icon) or tamper with it (to show the teacher a fake
  screen).
- **Heartbeat:** the agent sends its config version and the server holds the answer
  until something changes (max ~25 s). A teacher opening the Monitoring page reaches
  every PC in the room within a second, with one idle HTTPS request per PC meanwhile.
  Each heartbeat also reports the computer's own IP (the one it uses to reach SMP, so it's
  right behind a NAT too), so moving between rooms/VLANs works.
- **Monitoring traffic** (schools often have 100 Mbps): nothing at all unless someone
  has the room open. Then a 480 px JPEG thumbnail every 5 s (15-40 KB; a room of 30
  PCs is about 1-2 Mbit/s), an unchanged screen costs an empty request, and only the
  one PC a teacher enlarged sends a 1600 px picture every 2 s. All of these numbers are
  SMP settings (school-wide) and `classroom` policies (per room / lesson schedule).
- **Aspect ratio:** the picture keeps the screen's shape (16:9, 16:10, 4:3, 1:1, ...);
  only the primary display is captured.
- **Screen lock:** the config's `classroom` section carries `"lock": {"message": ...,
  "logo": {"sha256", "size", "url"}}` while the PC is locked (nothing otherwise). The
  logo (a PNG) is downloaded once with a signed request and kept in memory by its
  SHA-256; the lock doesn't wait for it (SMP's logo until it's there). The helper shows
  a topmost window over the whole virtual screen (every monitor), puts itself on top
  again every half second, and blocks input with low-level keyboard and mouse hooks on a
  thread of their own (`platform/win/lock_window.rs`, `input_block.rs`).

## Layout

```
agent/
  Cargo.toml                    workspace (one crate for now)
  Dockerfile.build              Rust + MinGW: builds the Linux binary and the Windows .exe
  .cargo/config.toml            Windows cross-compiling settings
  tools/make_tray_icons.py      the tray icons and the lock screen's SMP logo
  smp-agent/
    assets/                     tray icons (grey/colour, 16-32 px), lock-logo.png
    src/
      main.rs                   commands: join | run | status | install | uninstall | ...
      core/                     platform-independent, unit-tested on Linux
        identity.rs             the device's P-256 key, fingerprint, request signing
        state.rs                agent.json: server, wrapped key, certificate pin, device id
        tls.rs                  which server certificates are accepted (pinning)
        api.rs                  signed requests to the server: JSON, screen frames,
                                downloads (updates, the lock logo, installers; resumable)
        runner.rs               join -> wait for approval -> heartbeat loop
        presence.rs             logged-in user and own address, checked every second
        watchdog.rs             restarts the agent after 15 minutes without the server
        update.rs               update.json: an update in progress, confirmed or failed
        capability.rs           the Capability trait every feature implements
        sysinfo.rs              hostname, MAC, OS, logged-in user
      capabilities/
        mod.rs                  the list of capabilities this build runs
        screen/                 monitoring: config, timing, uploads (mod.rs),
                                shrink + JPEG (encode.rs), where pictures come from (source.rs)
        lock/                   screen lock: config, logo download (mod.rs),
                                logo PNG -> picture on the lock background (picture.rs)
        software/               software deployment: the worker (mod.rs), config,
                                decisions + exit codes (plan.rs), software.json (local.rs),
                                reports (report.rs), a fake installer for Linux (fake.rs)
        update.rs               automatic updates
      platform/                 OS-specific bits behind one interface
        generic.rs              Linux (development, tests, demo PCs with a fake screen)
        helper_protocol.rs      service <-> helper messages (tested on Linux)
        state_dir.rs            which folder holds the identity and logs (C:\ProgramData\SMP,
                                or SMP-1, ... when someone else made that one; tested on Linux)
        win/                    Windows: service, session helper, capture, tray, lock window,
                                input block, software (msiexec, registry, shutdown.exe),
                                DPAPI, ACLs (mod.rs explains how they fit together)
```

How a request is signed, and why there is no token: see the comment at the top of
`core/identity.rs` and `server/smp/modules/agent/crypto.py` (they must match).

Rust notes for reading the code: everything runs on plain threads (no async runtime);
threads talk through channels (`std::sync::mpsc`) or a `Mutex`; things shared between
threads are in an `Arc`. Windows API calls are `unsafe` (Rust can't check what the
OS does with pointers), so each one is small and has a `SAFETY:` comment saying why
it's fine.

## Adding a capability

1. Create `src/capabilities/<name>.rs` (or a folder) with a type that implements
   `core::capability::Capability`: `name()` = its name in logs and the config section
   it reads (`section()` when that differs: the lock reads `classroom`),
   `apply_config()` called on every change (keep it fast: hand work to your own
   thread, like `screen` does), `stop()`.
2. Add it to `capabilities::all()`.
3. On the server, the matching module returns that section from
   `Module.agent_config()` (see `docs/writing-a-module.md`, section 5).

## Building

For schools nothing is needed: `./install.sh` compiles the exe as part of the server
image (above). The first build takes a few extra minutes; after that Docker reuses the
compiled dependencies and rebuilds only when `agent/` changes. That stage and
`Dockerfile.build` pin the same toolchain; change both together.

For development, without the whole SMP stack: no Rust on the host is needed either;
everything builds in Docker with the pinned toolchain (`rust:1.100.0-slim-trixie` plus
the MinGW linker). A volume keeps the downloaded crates.

```sh
cd agent
docker build -t smp-agent-build -f Dockerfile.build .
docker volume create smp-cargo
docker run --rm -v smp-cargo:/cargo smp-agent-build chown $(id -u):$(id -g) /cargo
# The repository root is mounted (not just agent/): build.rs reads ../Version.toml
alias cargo-docker='docker run --rm --user $(id -u):$(id -g) -e CARGO_HOME=/cargo -v smp-cargo:/cargo -v "$PWD/..":/src -w /src/agent smp-agent-build cargo'

cargo-docker test                                               # unit tests (Linux)
cargo-docker clippy --all-targets                               # lints, Linux
cargo-docker clippy --target x86_64-pc-windows-gnu              # lints, Windows code
cargo-docker build --release --target x86_64-pc-windows-gnu     # the Windows .exe
```

The exe is `target/x86_64-pc-windows-gnu/release/smp-agent.exe`.

**Version:** the agent has no version in `Cargo.toml`. It is `[agent]` in
`../Version.toml` (the one file holding all SMP versions), compiled in by
`smp-agent/build.rs`: `smp-agent --version`, the heartbeats (Devices page) and the
download's `smp-agent.version` all show it. The build fails with a clear message if it
can't find the file; `SMP_VERSION_FILE=/path/to/Version.toml` points it elsewhere.

## Trying it on Linux

```sh
target/debug/smp-agent join --server https://<smp address>   # shows a fingerprint, waits
#   SMP -> Devices -> Waiting for approval: compare the fingerprint, Approve
SMP_AGENT_FAKE_SCREEN=1920x1080 target/debug/smp-agent run    # heartbeats + a test picture
target/debug/smp-agent status
```

`SMP_AGENT_STATE_DIR` changes where the identity is kept (default
`~/.local/state/smp-agent`), `SMP_AGENT_USER` the reported user, `RUST_LOG=debug`
shows every picture sent ("thumb: 8049 bytes" / "thumb: unchanged"). There is no real
lock on Linux: a locked fake screen shows the lock picture (background and logo, no
text) and the log says "LOCKED", so locking can be tried on the Monitoring page too. Several fake PCs
can run as containers with `--hostname LAB-PC02` and their own state directory.
`SMP_AGENT_FAKE_SOFTWARE=1` pretends to install software (nothing runs; the log says what
would, and SMP shows the results), `SMP_AGENT_FAKE_SOFTWARE_EXIT=1603` makes every
installation fail with that exit code.

## License

AGPL-3.0-or-later, like the rest of SMP: see [LICENSE](../LICENSE) and
[NOTICE](../NOTICE) in the repository root (`smp-agent --help` shows the notice too).
The exe includes the jpeg-encoder crate: this software is based in part on the work
of the Independent JPEG Group.
