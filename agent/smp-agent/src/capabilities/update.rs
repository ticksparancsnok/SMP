// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Automatic updates: the agent installs the version its SMP server offers.
//!
//! The server sends, in the "agent" section of the config, the build it has for this
//! platform ([agent] of SMP's Version.toml; server/smp/modules/agent/download.py):
//!
//!     "update": {"version": "0.1.2 A", "size": 3850240, "sha256": "...",
//!                "url": "/downloads/smp-agent.exe", "spread_s": 300}
//!
//! When that version differs from the running one (newer or older: the server is the
//! source of truth, e.g. after SMP itself went back a version), the worker thread
//!   1. waits a random time up to `spread_s` (a whole school shouldn't download at once),
//!   2. downloads the file from the same server, over the same pinned TLS, and checks
//!      its size and SHA-256 against the offer,
//!   3. runs it with `--version`: it must start and say exactly the offered version,
//!   4. records the pending update (core/update.rs) and hands over to the platform:
//!      Windows swaps the exe and starts a watcher that restarts the service and puts the
//!      old exe back if the new one doesn't reach the server within 3 minutes
//!      (platform/win/update.rs); elsewhere the process replaces itself.
//!
//! A download or check that fails is retried after 10 minutes (then 20, 40, up to 2 h).
//! A version that installed but didn't come up waits 6 hours (FAILED_RETRY_AFTER_S).
//!
//! TODO(evaluate, issue #17): every check above trusts the SMP server: whoever controls it
//! can install any code here, as SYSTEM, on every PC (also an older, vulnerable version:
//! downgrades are accepted). Options: builds signed with a key kept off the server, a
//! minimum version agents refuse to go below, and on the server an admin approval per
//! version or a staged rollout (server/smp/modules/agent/api.py, update_offer).

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::Value;

use crate::core::api::{Client, AGENT_VERSION};
use crate::core::capability::Capability;
use crate::core::update::{self, unix_now, Pending, UpdateState};

/// Biggest agent we accept (it is about 4 MB); the offer's size must match anyway.
const MAX_SIZE: u64 = 64 * 1024 * 1024;
const FIRST_RETRY: Duration = Duration::from_secs(10 * 60);
const MAX_RETRY: Duration = Duration::from_secs(2 * 3600);

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Offer {
    pub version: String,
    pub size: u64,
    pub sha256: String,
    pub url: String,
    #[serde(default)]
    pub spread_s: u64,
}

impl Offer {
    /// From the config's "agent" section; None if there's no (usable) offer.
    pub fn from_section(section: Option<&Value>) -> Option<Self> {
        let offer: Self = serde_json::from_value(section?.get("update")?.clone()).ok()?;
        // Only from our own server (a path), only a sane version string.
        let sane_version = !offer.version.is_empty()
            && offer.version.len() <= 32
            && offer.version.chars().all(|c| c.is_ascii_alphanumeric() || c == '.' || c == ' ');
        (offer.url.starts_with('/') && !offer.url.starts_with("//") && sane_version && offer.sha256.len() == 64)
            .then_some(offer)
    }
}

pub struct Updater {
    offers: Option<Sender<Option<Offer>>>,
}

impl Updater {
    pub fn new(client: Arc<Client>, state_dir: PathBuf) -> Self {
        let (tx, rx) = mpsc::channel::<Option<Offer>>();
        std::thread::Builder::new()
            .name("update".into())
            .spawn(move || worker(&client, &state_dir, &rx))
            .expect("starting the update thread");
        Self { offers: Some(tx) }
    }
}

impl Capability for Updater {
    fn name(&self) -> &'static str {
        "agent"
    }

    fn apply_config(&mut self, section: Option<&Value>) {
        if let Some(tx) = &self.offers {
            let _ = tx.send(Offer::from_section(section));
        }
    }

    fn stop(&mut self) {
        self.offers = None; // the worker sees the channel close and ends
    }
}

/// When to try the offered version: a random moment within its spread at first, later
/// after the retry pause.
struct Schedule {
    version: String,
    at: Instant,
    pause: Duration,
}

fn worker(client: &Client, state_dir: &Path, offers: &mpsc::Receiver<Option<Offer>>) {
    let state_path = update::path_in(state_dir);
    let mut offer: Option<Offer> = None;
    let mut schedule: Option<Schedule> = None;
    loop {
        match offers.recv_timeout(Duration::from_secs(30)) {
            Ok(latest) => offer = latest,
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => return,
        }
        crate::platform::update::clean_up(state_dir, &state_path);
        let Some(o) = &offer else {
            schedule = None;
            continue;
        };
        if !UpdateState::load(&state_path).wants(&o.version, unix_now()) {
            continue;
        }
        if schedule.as_ref().map(|s| s.version != o.version).unwrap_or(true) {
            let wait = Duration::from_secs(random_below(o.spread_s + 1));
            log::info!("SMP offers agent {} (this is {AGENT_VERSION}); installing it in {} s", o.version, wait.as_secs());
            schedule = Some(Schedule { version: o.version.clone(), at: Instant::now() + wait, pause: FIRST_RETRY });
        }
        let s = schedule.as_mut().expect("set above");
        if Instant::now() < s.at {
            continue;
        }
        match install(client, state_dir, &state_path, o) {
            Ok(()) => {
                // Windows: the watcher restarts the service any moment now. If it doesn't
                // (it couldn't start), `pending` keeps this version from being retried at
                // once; clean_up drops a stale one after a while.
                s.at = Instant::now() + s.pause;
            }
            Err(e) => {
                log::warn!("updating to {} failed: {e:#}; trying again in {} min", o.version, s.pause.as_secs() / 60);
                s.at = Instant::now() + s.pause;
                s.pause = (s.pause * 2).min(MAX_RETRY);
            }
        }
    }
}

fn install(client: &Client, state_dir: &Path, state_path: &Path, offer: &Offer) -> Result<()> {
    let staging = crate::platform::update::staging_path(state_dir)?;
    if let Some(dir) = staging.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let _ = std::fs::remove_file(&staging);
    log::info!("downloading agent {} ({} bytes)", offer.version, offer.size);
    let checked = (|| -> Result<()> {
        let (size, sha256) = client.download(&offer.url, &staging, offer.size.min(MAX_SIZE))?;
        if size != offer.size || !sha256.eq_ignore_ascii_case(&offer.sha256) {
            bail!("the download doesn't match what the server announced (size {size}, SHA-256 {sha256})");
        }
        make_executable(&staging)?;
        reports_version(&staging, &offer.version)
    })();
    if let Err(e) = checked {
        let _ = std::fs::remove_file(&staging);
        return Err(e);
    }
    let mut state = UpdateState::load(state_path);
    state.pending = Some(Pending { from: AGENT_VERSION.into(), to: offer.version.clone(), since: unix_now(), confirmed: false });
    state.save(state_path).context("recording the update")?;
    log::info!("agent {} checked; installing it (this agent {AGENT_VERSION} hands over)", offer.version);
    if let Err(e) = crate::platform::update::install(&staging, &offer.version, state_dir) {
        let mut state = UpdateState::load(state_path);
        state.pending = None;
        let _ = state.save(state_path);
        let _ = std::fs::remove_file(&staging);
        return Err(e);
    }
    Ok(())
}

/// Run `exe --version`: it must start here and say "smp-agent <expected>".
pub fn reports_version(exe: &Path, expected: &str) -> Result<()> {
    let mut command = Command::new(exe);
    command.arg("--version").stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = command.spawn().context("the new version doesn't start")?;
    let deadline = Instant::now() + Duration::from_secs(20);
    while child.try_wait()?.is_none() {
        if Instant::now() > deadline {
            let _ = child.kill();
            bail!("the new version didn't answer --version within 20 s");
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let mut out = String::new();
    std::io::Read::read_to_string(&mut child.stdout.take().context("no output")?, &mut out)?;
    let said = out.trim();
    if said != format!("smp-agent {expected}") {
        bail!("the new version says {said:?}, not \"smp-agent {expected}\"");
    }
    Ok(())
}

fn make_executable(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))?;
    }
    let _ = path;
    Ok(())
}

fn random_below(n: u64) -> u64 {
    use rand_core::{OsRng, RngCore};
    if n <= 1 {
        0
    } else {
        OsRng.next_u64() % n
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn section(update: Value) -> Value {
        serde_json::json!({"heartbeat_wait_s": 25, "update": update})
    }

    #[test]
    fn offers_are_read_from_the_agent_section() {
        let good = section(serde_json::json!({
            "version": "0.1.2 A", "size": 10, "sha256": "a".repeat(64), "url": "/downloads/smp-agent.exe", "spread_s": 300
        }));
        let offer = Offer::from_section(Some(&good)).unwrap();
        assert_eq!((offer.version.as_str(), offer.spread_s), ("0.1.2 A", 300));
        assert_eq!(Offer::from_section(None), None);
        assert_eq!(Offer::from_section(Some(&serde_json::json!({"heartbeat_wait_s": 25}))), None);
        for bad in [
            serde_json::json!({"version": "1", "size": 1, "sha256": "a".repeat(64), "url": "https://evil.example/x"}),
            serde_json::json!({"version": "1", "size": 1, "sha256": "a".repeat(64), "url": "//evil.example/x"}),
            serde_json::json!({"version": "1;rm -rf", "size": 1, "sha256": "a".repeat(64), "url": "/x"}),
            serde_json::json!({"version": "1", "size": 1, "sha256": "short", "url": "/x"}),
        ] {
            assert_eq!(Offer::from_section(Some(&section(bad.clone()))), None, "{bad}");
        }
    }

    #[cfg(unix)]
    #[test]
    fn the_version_check_runs_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("fake-agent");
        std::fs::write(&script, "#!/bin/sh\necho 'smp-agent 9.9.9 Z'\n").unwrap();
        make_executable(&script).unwrap();
        reports_version(&script, "9.9.9 Z").unwrap();
        let err = reports_version(&script, "9.9.8").unwrap_err().to_string();
        assert!(err.contains("says \"smp-agent 9.9.9 Z\""), "{err}");
        let junk = dir.path().join("junk");
        std::fs::write(&junk, b"\x00\x01not a program").unwrap();
        make_executable(&junk).unwrap();
        assert!(reports_version(&junk, "9.9.9 Z").is_err());
    }

    #[test]
    fn spread() {
        assert_eq!(random_below(0), 0);
        assert_eq!(random_below(1), 0);
        assert!((0..100).all(|_| random_below(5) < 5));
    }
}
