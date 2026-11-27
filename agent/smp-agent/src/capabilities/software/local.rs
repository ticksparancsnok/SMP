// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! What this computer remembers about its software (software.json, next to agent.json).
//! It survives restarts, so that:
//!   - an upgrade is recognised: the file we installed differs from the offered one,
//!   - a failing installer isn't retried every half hour (and doesn't re-download),
//!   - .msi product codes read from a file are known later (to detect and remove),
//!   - "restart pending" is known until the computer actually restarted.
//!
//! Losing the file costs little: installed programs are found by their detection
//! rules and taken over again.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Local {
    /// package id -> what we installed
    pub installed: BTreeMap<String, Installed>,
    /// SHA-256 of an .msi file -> its product code (read once from the file)
    pub product_codes: BTreeMap<String, String>,
    /// package id -> the last failure, until the package works or changes
    pub failures: BTreeMap<String, Failure>,
    /// Boot time (unix seconds) when we last asked Windows to restart: once per boot.
    pub restart_requested_boot: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Installed {
    pub sha256: String,
    /// .msi: its product code, so it can be found and removed later
    pub product_code: Option<String>,
    /// Set when the installation needs a restart: the boot time it happened in. A
    /// different boot time later means the computer has restarted since.
    pub restart_pending_boot: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Failure {
    /// Fingerprint of the package as it failed (file + arguments + rules): an admin
    /// who changes the package gets a new try at once.
    pub recipe: String,
    pub state: String,
    pub exit_code: Option<i64>,
    pub message: String,
    pub attempts: u32,
    /// unix seconds
    pub retry_at: u64,
}

pub fn path_in(state_dir: &Path) -> PathBuf {
    state_dir.join("software.json")
}

/// Boot times are approximate (now minus uptime): within this, it's the same boot.
const SAME_BOOT_S: u64 = 120;

pub fn same_boot(a: u64, b: u64) -> bool {
    a.abs_diff(b) <= SAME_BOOT_S
}

impl Local {
    /// A missing or unreadable file is an empty one (see the top of this file).
    pub fn load(path: &Path) -> Self {
        std::fs::read(path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
    }

    /// Atomically (temp file + rename), readable by SYSTEM/administrators only.
    pub fn save(&self, path: &Path) -> Result<()> {
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self)?)?;
        crate::platform::restrict_file(&tmp)?;
        std::fs::rename(&tmp, path).with_context(|| format!("writing {}", path.display()))
    }

    /// Is a restart still owed for this package (it needed one, and the computer
    /// hasn't restarted since)?
    pub fn restart_pending(&self, package_id: &str, boot: Option<u64>) -> bool {
        let pending = self.installed.get(package_id).and_then(|i| i.restart_pending_boot);
        matches!((pending, boot), (Some(p), Some(now)) if same_boot(p, now))
    }

    /// The product code of an .msi, from what we know (installed record, or the file).
    pub fn known_product_code(&self, package_id: &str, sha256: &str) -> Option<String> {
        self.product_codes
            .get(sha256)
            .cloned()
            .or_else(|| self.installed.get(package_id).and_then(|i| i.product_code.clone()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn save_load_and_restarts() {
        let dir = tempfile::tempdir().unwrap();
        let path = path_in(dir.path());
        assert_eq!(Local::load(&path), Local::default());
        let mut local = Local::default();
        local.installed.insert("p".into(), Installed {
            sha256: "a".repeat(64), product_code: Some("{X}".into()), restart_pending_boot: Some(1000),
        });
        local.product_codes.insert("b".repeat(64), "{Y}".into());
        local.save(&path).unwrap();
        let loaded = Local::load(&path);
        assert_eq!(loaded, local);
        assert!(loaded.restart_pending("p", Some(1050)), "same boot (boot times are approximate)");
        assert!(!loaded.restart_pending("p", Some(5000)), "restarted since");
        assert!(!loaded.restart_pending("p", None));
        assert!(!loaded.restart_pending("other", Some(1000)));
        assert_eq!(loaded.known_product_code("p", &"b".repeat(64)).as_deref(), Some("{Y}"));
        assert_eq!(loaded.known_product_code("p", &"c".repeat(64)).as_deref(), Some("{X}"));
        std::fs::write(&path, b"garbage").unwrap();
        assert_eq!(Local::load(&path), Local::default());
    }
}
