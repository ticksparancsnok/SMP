// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Installers that only pretend, for development and test "PCs" on Linux
//! (SMP_AGENT_FAKE_SOFTWARE=1; SMP_AGENT_FAKE_SOFTWARE_EXIT=1603 makes every run fail
//! with that exit code), and for the unit tests.
//!
//! Nothing is run. Which programs are "installed" is kept in a small JSON file, so it
//! survives restarts like real software, and an admin can watch a whole rollout on the
//! Software page with a few containers. A file detection rule is also true when the
//! file really exists.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::config::{Action, Detection, Package};
use super::plan::Invocation;
use super::Installer;
use crate::core::identity::hex;

#[derive(Default, Serialize, Deserialize)]
#[serde(default)]
struct FakeState {
    /// detection rules (as JSON) or product codes that count as installed
    installed: BTreeSet<String>,
    exit_code: i32,
}

pub struct FakeInstaller {
    path: PathBuf,
}

impl FakeInstaller {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    fn load(&self) -> FakeState {
        std::fs::read(&self.path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
    }

    fn save(&self, state: &FakeState) {
        if let Err(e) = std::fs::write(&self.path, serde_json::to_vec_pretty(state).unwrap_or_default()) {
            log::warn!("fake software: can't write {}: {e}", self.path.display());
        }
    }

    fn key(detection: &Detection, product_code: Option<&str>) -> String {
        match detection {
            Detection::Msi { product_code: own } => own.as_deref().or(product_code).unwrap_or("").to_string(),
            other => serde_json::to_string(other).unwrap_or_default(),
        }
    }

    #[cfg(test)]
    pub fn mark_installed(&self, detection: &Detection) {
        let mut state = self.load();
        state.installed.insert(Self::key(detection, None));
        self.save(&state);
    }

    #[cfg(test)]
    pub fn set_exit_code(&self, code: i32) {
        let mut state = self.load();
        state.exit_code = code;
        self.save(&state);
    }
}

impl Installer for FakeInstaller {
    fn is_installed(&self, detection: &Detection, product_code: Option<&str>) -> Result<bool> {
        if let Detection::Msi { product_code: None } = detection {
            if product_code.is_none() {
                bail!("no product code");
            }
        }
        if let Detection::File { path } = detection {
            if Path::new(path).exists() {
                return Ok(true);
            }
        }
        Ok(self.load().installed.contains(&Self::key(detection, product_code)))
    }

    /// A made-up code from the file's content (stable, like a real one).
    fn msi_product_code(&self, msi: &Path) -> Result<String> {
        let h = hex(&Sha256::digest(std::fs::read(msi)?)).to_uppercase();
        Ok(format!("{{{}-{}-{}-{}-{}}}", &h[..8], &h[8..12], &h[12..16], &h[16..20], &h[20..32]))
    }

    fn run(&self, package: &Package, invocation: &Invocation, _timeout: Duration) -> Result<Option<i32>> {
        let mut state = self.load();
        let exit_code = std::env::var("SMP_AGENT_FAKE_SOFTWARE_EXIT").ok().and_then(|c| c.parse().ok()).unwrap_or(state.exit_code);
        log::info!("fake software: would run {} {:?} {} -> exit code {exit_code}", invocation.program, invocation.args, invocation.raw);
        if exit_code == 0 {
            // What the real installer's own detection would see afterwards. An .msi
            // rule without a code: the code comes from the file (in the arguments).
            let code = match invocation.args.first().map(String::as_str) {
                Some("/i") => invocation.args.get(1).and_then(|file| self.msi_product_code(Path::new(file)).ok()),
                Some("/x") => invocation.args.get(1).cloned(),
                _ => None,
            };
            let key = Self::key(&package.detection, code.as_deref());
            match package.action {
                Action::Install => state.installed.insert(key),
                Action::Remove => state.installed.remove(&key),
            };
            self.save(&state);
        }
        Ok(Some(exit_code))
    }

    fn boot_time(&self) -> Option<u64> {
        // Linux: "btime <unix seconds>" in /proc/stat
        let stat = std::fs::read_to_string("/proc/stat").ok()?;
        stat.lines().find_map(|l| l.strip_prefix("btime ")).and_then(|v| v.trim().parse().ok())
    }

    fn schedule_restart(&self, after: Duration, programs: &[String]) -> Result<()> {
        log::info!("fake software: would restart in {} s for {}", after.as_secs(), programs.join(", "));
        Ok(())
    }
}

/// On a computer that can't install anything (Linux without the fake): every package
/// fails with a clear message instead of being silently ignored.
#[cfg(not(windows))]
pub struct Unsupported;

#[cfg(not(windows))]
impl Installer for Unsupported {
    fn is_installed(&self, _: &Detection, _: Option<&str>) -> Result<bool> {
        bail!("software installation only works on Windows (SMP_AGENT_FAKE_SOFTWARE=1 pretends, for tests)")
    }
    fn msi_product_code(&self, _: &Path) -> Result<String> {
        bail!("no Windows Installer here")
    }
    fn run(&self, _: &Package, _: &Invocation, _: Duration) -> Result<Option<i32>> {
        bail!("software installation only works on Windows")
    }
    fn boot_time(&self) -> Option<u64> {
        None
    }
    fn schedule_restart(&self, _: Duration, _: &[String]) -> Result<()> {
        bail!("no restarts from here")
    }
}
