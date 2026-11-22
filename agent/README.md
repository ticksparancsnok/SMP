# SMP agent (Rust)

The program that runs on the school's computers. It asks the SMP server to manage
the computer (an admin approves it), keeps a heartbeat, and runs **capabilities**:
screen monitoring now, later screen lock, software installation and so on.

One `smp-agent.exe` (about 4 MB, no runtime or DLLs to install) does everything; what
it does depends on the command it's started with.

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

## How it works

```
                      C:\Program Files\SMP\smp-agent.exe
 ┌───────────────────────────── session 0 ─────────────────────────────┐
 │ service (SYSTEM): join -> heartbeat (long poll) -> capabilities      │
 │   screen capability: worker thread, uploads JPEGs to SMP             │
 └──────────────┬───────────────────────────────────────────────────────┘
                │ private named pipe (SYSTEM/admins only, checked process id)
 ┌──────────────┴──────────── user's session ──────────────────────────┐
 │ session-helper (SYSTEM, on the user's desktop): tray icon,           │
 │   screen capture of the primary display + shrink + JPEG              │
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
  Each heartbeat also reports the current IP, so moving between rooms/VLANs works.
- **Monitoring traffic** (schools often have 100 Mbps): nothing at all unless someone
  has the room open. Then a 480 px JPEG thumbnail every 5 s (15-40 KB; a room of 30
  PCs is about 1-2 Mbit/s), an unchanged screen costs an empty request, and only the
  one PC a teacher enlarged sends a 1600 px picture every 2 s. All of these numbers are
  SMP settings (school-wide) and `classroom` policies (per room / lesson schedule).
- **Aspect ratio:** the picture keeps the screen's shape (16:9, 16:10, 4:3, 1:1, ...);
  only the primary display is captured.

## Layout

```
agent/
  Cargo.toml                    workspace (one crate for now)
  Dockerfile.build              Rust + MinGW: builds the Linux binary and the Windows .exe
  .cargo/config.toml            Windows cross-compiling settings
  tools/make_tray_icons.py      the tray icons from the SMP logo
  smp-agent/
    assets/                     tray icons (grey/colour, 16-32 px)
    src/
      main.rs                   commands: join | run | status | install | uninstall | ...
      core/                     platform-independent, unit-tested on Linux
        identity.rs             the device's P-256 key, fingerprint, request signing
        state.rs                agent.json: server, wrapped key, certificate pin, device id
        tls.rs                  which server certificates are accepted (pinning)
        api.rs                  signed requests to the server (JSON, screen frames)
        runner.rs               join -> wait for approval -> heartbeat loop
        capability.rs           the Capability trait every feature implements
        sysinfo.rs              hostname, MAC, OS, logged-in user
      capabilities/
        mod.rs                  the list of capabilities this build runs
        screen/                 monitoring: config, timing, uploads (mod.rs),
                                shrink + JPEG (encode.rs), where pictures come from (source.rs)
      platform/                 OS-specific bits behind one interface
        generic.rs              Linux (development, tests, demo PCs with a fake screen)
        helper_protocol.rs      service <-> helper messages (tested on Linux)
        win/                    Windows: service, session helper, capture, tray, DPAPI, ACLs
                                (mod.rs explains how they fit together)
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
   `core::capability::Capability`: `name()` = the config section it reads,
   `apply_config()` called on every change (keep it fast: hand work to your own
   thread, like `screen` does), `stop()`.
2. Add it to `capabilities::all()`.
3. On the server, the matching module returns that section from
   `Module.agent_config()` (see `docs/writing-a-module.md`, section 5).

## Building

No Rust on the host is needed; everything builds in Docker with the pinned toolchain
(`rust:1.100.0-slim-trixie` plus the MinGW linker). A volume keeps the downloaded
crates.

```sh
cd agent
docker build -t smp-agent-build -f Dockerfile.build .
docker volume create smp-cargo
docker run --rm -v smp-cargo:/cargo smp-agent-build chown $(id -u):$(id -g) /cargo
alias cargo-docker='docker run --rm --user $(id -u):$(id -g) -e CARGO_HOME=/cargo -v smp-cargo:/cargo -v "$PWD":/src -w /src smp-agent-build cargo'

cargo-docker test                                               # unit tests (Linux)
cargo-docker clippy --all-targets                               # lints, Linux
cargo-docker clippy --target x86_64-pc-windows-gnu              # lints, Windows code
cargo-docker build --release --target x86_64-pc-windows-gnu     # the Windows .exe
```

The exe is `target/x86_64-pc-windows-gnu/release/smp-agent.exe`.

## Trying it on Linux

```sh
target/debug/smp-agent join --server https://<smp address>   # shows a fingerprint, waits
#   SMP -> Devices -> Waiting for approval: compare the fingerprint, Approve
SMP_AGENT_FAKE_SCREEN=1920x1080 target/debug/smp-agent run    # heartbeats + a test picture
target/debug/smp-agent status
```

`SMP_AGENT_STATE_DIR` changes where the identity is kept (default
`~/.local/state/smp-agent`), `SMP_AGENT_USER` the reported user, `RUST_LOG=debug`
shows every picture sent ("thumb: 8049 bytes" / "thumb: unchanged"). Several fake PCs
can run as containers with `--hostname LAB-PC02` and their own state directory.
