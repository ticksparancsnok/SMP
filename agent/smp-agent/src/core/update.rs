// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! What an agent update is up to, between the processes involved (update.json, next to
//! agent.json). The steps are in capabilities/update.rs; the gist:
//!
//!   1. the running agent downloads and checks the new version, then records `pending`
//!      (from -> to) and hands over to the platform (Windows: a watcher process
//!      restarts the service with the new exe; elsewhere: the process replaces itself)
//!   2. the new version, once it has reached the server, sets `pending.confirmed`
//!   3. without that within a few minutes the watcher puts the old exe back and records
//!      `failed` (that version isn't tried again for a while)
//!   4. leftovers (the old exe) are removed once confirmed, then `pending` is cleared

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::core::api::AGENT_VERSION;

/// How long a version that failed to come up isn't tried again.
#[cfg_attr(not(windows), allow(dead_code))] // only the Windows watcher can undo an update
pub const FAILED_RETRY_AFTER_S: u64 = 6 * 3600;

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct UpdateState {
    pub pending: Option<Pending>,
    pub failed: Option<Failed>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Pending {
    pub from: String,
    pub to: String,
    /// unix seconds
    pub since: u64,
    /// The new version reached the server.
    pub confirmed: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Failed {
    pub version: String,
    /// unix seconds; tried again after this
    pub until: u64,
    pub reason: String,
}

pub fn path_in(state_dir: &Path) -> PathBuf {
    state_dir.join("update.json")
}

pub fn unix_now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

impl UpdateState {
    /// A missing or unreadable file is an empty state (it's only bookkeeping).
    pub fn load(path: &Path) -> Self {
        std::fs::read(path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
    }

    /// Atomically (temp file + rename), like agent.json.
    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self)?)?;
        crate::platform::restrict_file(&tmp)?;
        std::fs::rename(&tmp, path).with_context(|| format!("writing {}", path.display()))
    }

    /// Should `offered` be installed now? Not if it's what runs, while an update is in
    /// flight, or while it's in its retry pause after failing to come up.
    pub fn wants(&self, offered: &str, now: u64) -> bool {
        if offered == AGENT_VERSION {
            return false;
        }
        if self.pending.as_ref().is_some_and(|p| !p.confirmed && p.from == AGENT_VERSION && p.to == offered) {
            return false; // already on its way (the watcher restarts us)
        }
        !self.failed.as_ref().is_some_and(|f| f.version == offered && now < f.until)
    }

    /// Called by the running version once it reached the server. True if this run is
    /// the new version of an update (it confirms it).
    pub fn confirm_running(&mut self) -> bool {
        match &mut self.pending {
            Some(p) if p.to == AGENT_VERSION && !p.confirmed => {
                p.confirmed = true;
                true
            }
            _ => false,
        }
    }

    /// The update didn't come up: back to `from`, don't try `to` again for a while.
    #[cfg_attr(not(windows), allow(dead_code))]
    pub fn record_failure(&mut self, to: &str, reason: &str, now: u64) {
        self.pending = None;
        self.failed = Some(Failed { version: to.to_string(), until: now + FAILED_RETRY_AFTER_S, reason: reason.to_string() });
    }
}

/// After a successful heartbeat: confirm a pending update to this version.
pub fn confirm(state_dir: &Path) {
    let path = path_in(state_dir);
    let mut state = UpdateState::load(&path);
    if state.confirm_running() {
        let from = state.pending.as_ref().map(|p| p.from.clone()).unwrap_or_default();
        match state.save(&path) {
            Ok(()) => log::info!("update {from} -> {AGENT_VERSION} done: this version reached the server"),
            Err(e) => log::warn!("couldn't record the finished update: {e:#}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pending(from: &str, to: &str, confirmed: bool) -> Option<Pending> {
        Some(Pending { from: from.into(), to: to.into(), since: 0, confirmed })
    }

    #[test]
    fn which_offers_are_installed() {
        let now = 1_000_000;
        let empty = UpdateState::default();
        assert!(!empty.wants(AGENT_VERSION, now), "the running version");
        assert!(empty.wants("99.0.0", now), "newer");
        assert!(empty.wants("0.0.1", now), "older too: the server is the source of truth");

        let in_flight = UpdateState { pending: pending(AGENT_VERSION, "99.0.0", false), failed: None };
        assert!(!in_flight.wants("99.0.0", now));
        assert!(in_flight.wants("99.0.1", now), "another version than the one in flight");

        let mut failed = UpdateState::default();
        failed.record_failure("99.0.0", "didn't connect", now);
        assert!(!failed.wants("99.0.0", now + 60));
        assert!(failed.wants("99.0.0", now + FAILED_RETRY_AFTER_S + 1), "tried again later");
        assert!(failed.wants("99.0.1", now), "a different version is tried at once");
    }

    #[test]
    fn the_new_version_confirms_only_its_own_update() {
        let mut mine = UpdateState { pending: pending("0.0.1", AGENT_VERSION, false), failed: None };
        assert!(mine.confirm_running());
        assert!(mine.pending.as_ref().unwrap().confirmed);
        assert!(!mine.confirm_running(), "only once");

        let mut theirs = UpdateState { pending: pending(AGENT_VERSION, "99.0.0", false), failed: None };
        assert!(!theirs.confirm_running(), "the old version running again must not confirm");
    }

    #[test]
    fn save_and_load() {
        let dir = tempfile::tempdir().unwrap();
        let path = path_in(dir.path());
        assert_eq!(UpdateState::load(&path), UpdateState::default());
        let mut state = UpdateState { pending: pending("a", "b", false), failed: None };
        state.save(&path).unwrap();
        assert_eq!(UpdateState::load(&path), state);
        std::fs::write(&path, b"not json").unwrap();
        assert_eq!(UpdateState::load(&path), UpdateState::default(), "garbage = empty");
        state.record_failure("b", "x", 5);
        assert!(state.pending.is_none() && state.failed.as_ref().unwrap().until == 5 + FAILED_RETRY_AFTER_S);
    }
}
