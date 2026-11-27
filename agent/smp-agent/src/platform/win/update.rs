// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Installing an agent update on Windows (the checks happen before: capabilities/update.rs).
//!
//! Windows can't overwrite a running exe, but it can rename one. So, in
//! C:\Program Files\SMP (only administrators can write there; we run as SYSTEM):
//!
//!   1. smp-agent.exe (running) -> smp-agent.old.exe, the checked download -> smp-agent.exe
//!   2. start `smp-agent.old.exe update-watch --to <version>`: the watcher, our own (old)
//!      code, outside the service. It stops the service and starts it again: the new exe
//!      now runs, with a new session helper (tray icon) too.
//!   3. the new version confirms in update.json once it reached the server; the watcher
//!      sees that and ends. Later the new version deletes smp-agent.old.exe (clean_up).
//!   4. no confirmation within 3 minutes: the watcher stops the service, puts the old exe
//!      back (the new one is kept as smp-agent.failed.exe until the next clean-up),
//!      records the failure (that version waits 6 hours) and starts the service again.
//!
//! Left alone (e.g. the PC was switched off right after step 1): the new exe simply runs
//! at the next start and confirms; if the watcher never restarted the service, clean_up
//! notices the stale pending update after 10 minutes and restarts it.

use std::ffi::OsStr;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use windows_service::service::ServiceAccess;
use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};

use super::service::{stop_and_wait, SERVICE_NAME};
use crate::core::api::AGENT_VERSION;
use crate::core::update::{self, unix_now, UpdateState};

const OLD: &str = "smp-agent.old.exe";
const FAILED: &str = "smp-agent.failed.exe";
const DOWNLOAD: &str = "smp-agent.download.exe";
const CONFIRM_WITHIN: Duration = Duration::from_secs(180);
const STALE_AFTER_S: u64 = 600;

const DETACHED_PROCESS: u32 = 0x0000_0008;
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;

fn program_dir() -> Result<PathBuf> {
    crate::platform::exe_path().parent().map(Path::to_path_buf).context("the agent's folder")
}

/// Next to the exe (Program Files: not writable by users), so the swap is a rename.
pub fn staging_path(_state_dir: &Path) -> Result<PathBuf> {
    Ok(program_dir()?.join(DOWNLOAD))
}

pub fn install(staging: &Path, to: &str, state_dir: &Path) -> Result<()> {
    let dir = program_dir()?;
    let exe = dir.join("smp-agent.exe");
    let old = dir.join(OLD);
    if old.exists() {
        std::fs::remove_file(&old).context("an earlier update's smp-agent.old.exe is still in use")?;
    }
    std::fs::rename(&exe, &old).context("moving the running agent aside")?;
    if let Err(e) = std::fs::rename(staging, &exe) {
        let _ = std::fs::rename(&old, &exe);
        return Err(e).context("putting the new agent in place");
    }
    // The watcher must outlive this service process: its own process group, no console,
    // and out of any job the service runs in (if that job allows it).
    let state = state_dir.join("agent.json");
    let spawn = |flags: u32| {
        Command::new(&old)
            .arg("--state")
            .arg(&state)
            .args(["update-watch", "--to", to])
            .creation_flags(flags)
            .spawn()
    };
    let spawned = spawn(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB)
        .or_else(|_| spawn(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP));
    if let Err(e) = spawned {
        let _ = std::fs::rename(&exe, staging);
        let _ = std::fs::rename(&old, &exe);
        return Err(e).context("starting the update watcher");
    }
    log::info!("agent {to} is in place; the watcher restarts the service with it");
    Ok(())
}

/// `smp-agent update-watch --to <version>` (started by `install`, never by hand).
pub fn watch(to: &str, state_path: &Path) -> Result<()> {
    let state_dir = state_path.parent().context("state folder")?;
    let update_path = update::path_in(state_dir);
    let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)?;
    let service = manager.open_service(SERVICE_NAME, ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::START)?;

    log::info!("update {AGENT_VERSION} -> {to}: restarting the service");
    stop_and_wait(&service)?;
    service.start(&[] as &[&OsStr]).context("starting the service with the new agent")?;

    let deadline = Instant::now() + CONFIRM_WITHIN;
    while Instant::now() < deadline {
        std::thread::sleep(Duration::from_secs(2));
        if UpdateState::load(&update_path).pending.is_some_and(|p| p.to == to && p.confirmed) {
            log::info!("update to {to} done: the new agent reached the server");
            return Ok(());
        }
    }

    log::error!("agent {to} didn't reach the server within {} s; going back to {AGENT_VERSION}", CONFIRM_WITHIN.as_secs());
    stop_and_wait(&service)?;
    let dir = program_dir()?; // we run as smp-agent.old.exe, in the same folder
    let exe = dir.join("smp-agent.exe");
    let failed = dir.join(FAILED);
    let _ = std::fs::remove_file(&failed);
    std::fs::rename(&exe, &failed).context("moving the new agent aside")?;
    std::fs::rename(crate::platform::exe_path(), &exe).context("putting the old agent back")?;
    let mut state = UpdateState::load(&update_path);
    state.record_failure(to, &format!("didn't reach the server within {} s", CONFIRM_WITHIN.as_secs()), unix_now());
    state.save(&update_path)?;
    service.start(&[] as &[&OsStr]).context("starting the service with the old agent again")?;
    log::info!("back at agent {AGENT_VERSION}; {to} is tried again in 6 hours (or when the server offers another version)");
    Ok(())
}

/// Every 30 s from the update thread: remove leftovers once nothing needs them, and
/// finish an update the watcher never restarted us for.
pub fn clean_up(_state_dir: &Path, state_path: &Path) {
    let Ok(dir) = program_dir() else { return };
    let mut state = UpdateState::load(state_path);
    match &state.pending {
        Some(p) if p.confirmed && p.to == AGENT_VERSION => {
            // The watcher may still be running from smp-agent.old.exe for a moment.
            let gone = |name: &str| std::fs::remove_file(dir.join(name)).is_ok() || !dir.join(name).exists();
            if gone(OLD) && gone(FAILED) && gone(DOWNLOAD) {
                state.pending = None;
                let _ = state.save(state_path);
            }
        }
        Some(p) if !p.confirmed && p.from == AGENT_VERSION && unix_now().saturating_sub(p.since) > STALE_AFTER_S => {
            log::warn!("the update to {} didn't restart the agent; restarting it now", p.to);
            super::restart_self();
        }
        Some(_) => {}
        None => {
            for name in [OLD, FAILED, DOWNLOAD] {
                let _ = std::fs::remove_file(dir.join(name));
            }
        }
    }
}
