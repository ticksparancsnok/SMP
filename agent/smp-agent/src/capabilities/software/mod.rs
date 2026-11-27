// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Software deployment: install and remove the programs SMP assigns to this computer.
//!
//! The server's "software" module sends the list (config.rs has the format): per
//! package what to do (install / remove), the installer's size and SHA-256, its
//! arguments, how to tell it's installed, how to remove it, and whether to restart.
//! Admins decide all of it; whatever a package contains runs here as SYSTEM.
//!
//! A worker thread goes through the list in "rounds": shortly after the list changes
//! (a random wait up to `spread_s`, so a room doesn't start at once), and every 30
//! minutes (to notice a program someone uninstalled, and to retry). Per package:
//!
//!   1. is it installed? (the package's detection rule, plan::decide)
//!   2. if something needs doing: download the installer from SMP (signed, resumable;
//!      SMP lets a few computers download at once, the others wait their turn),
//!      check size and SHA-256, run it without a window and without a shell, wait
//!      at most its timeout, read the exit code (plan::outcome)
//!   3. check again that it's (not) installed, and report the result to SMP
//!
//! One package at a time: Windows Installer runs one installation at a time anyway.
//! A failure waits 1, 2, 4, ... up to 24 hours before the next try (or until an admin
//! changes the package). After the round, if a package asks for it (reboot: always,
//! or if_required and the installer said so), Windows is asked to restart, with
//! `reboot_warning_s` of warning for whoever is logged on.
//!
//! What it never does: uninstall a program only because it's no longer assigned
//! (only a "remove" assignment does), or restart a computer for a package whose
//! restart setting is "never" (that shows as reboot_pending in SMP instead).
//!
//! The doing is behind the `Installer` trait: Windows (platform/win/software.rs:
//! msiexec, the registry, shutdown.exe), or a fake for test computers on Linux
//! (fake.rs, SMP_AGENT_FAKE_SOFTWARE=1).

pub mod config;
#[cfg(any(not(windows), test))]
pub mod fake;
pub mod local;
pub mod plan;
pub mod report;

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender, TryRecvError};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::Result;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::core::api::{ApiError, Client};
use crate::core::capability::Capability;
use crate::core::identity::hex;
use config::{Action, Detection, Kind, Package, SoftwareConfig};
use local::{Failure, Installed, Local};
use plan::{Invocation, Outcome, Step};
use report::{Report, Reporter};

/// How often the list is gone through without a change (detects uninstalled programs).
const RECHECK: Duration = Duration::from_secs(30 * 60);
/// Another installation was running (exit code 1618): try again after this.
const BUSY_RETRY: Duration = Duration::from_secs(5 * 60);
/// A download that broke off continues after this.
const DOWNLOAD_RETRY: Duration = Duration::from_secs(2 * 60);
/// Nobody logged on: restart after this short pause instead of the warning time.
const RESTART_NOBODY_THERE: Duration = Duration::from_secs(30);

/// Where the doing happens (see the top of this file).
pub trait Installer: Send {
    /// Does the detection rule find the program? `product_code`: for an .msi rule
    /// without its own code (read from the file).
    fn is_installed(&self, detection: &Detection, product_code: Option<&str>) -> Result<bool>;
    /// The ProductCode property of an .msi file.
    fn msi_product_code(&self, msi: &Path) -> Result<String>;
    /// Run it and wait at most `timeout`. Ok(Some(exit code)), or Ok(None): it didn't
    /// finish in time and was stopped. `package` is only for logs (and the fake).
    fn run(&self, package: &Package, invocation: &Invocation, timeout: Duration) -> Result<Option<i32>>;
    /// When this computer started (unix seconds, approximate).
    fn boot_time(&self) -> Option<u64>;
    /// Ask the system to restart after `after`, telling whoever is logged on that
    /// `programs` were installed.
    fn schedule_restart(&self, after: Duration, programs: &[String]) -> Result<()>;
}

pub struct Software {
    configs: Option<Sender<Option<SoftwareConfig>>>,
}

impl Software {
    pub fn new(client: Arc<Client>, state_dir: PathBuf, installer: Box<dyn Installer>) -> Self {
        let (tx, rx) = mpsc::channel::<Option<SoftwareConfig>>();
        std::thread::Builder::new()
            .name("software".into())
            .spawn(move || Worker::new(client, &state_dir, installer).run(&rx))
            .expect("starting the software thread");
        Self { configs: Some(tx) }
    }
}

impl Capability for Software {
    fn name(&self) -> &'static str {
        "software"
    }

    fn apply_config(&mut self, section: Option<&Value>) {
        if let Some(tx) = &self.configs {
            let _ = tx.send(SoftwareConfig::from_section(section));
        }
    }

    fn stop(&mut self) {
        // The worker sees the channel close and ends after the package it's on. An
        // installer that is running keeps running (stopping an installation halfway
        // would leave a broken program); the next start finds it by its rule.
        self.configs = None;
    }
}

/// How a round of the worker ended.
enum RoundEnd {
    /// Go through the list again after this.
    Again(Duration),
    /// The list changed meanwhile.
    NewConfig(Option<SoftwareConfig>),
    /// The agent is stopping.
    Stopped,
}

/// Why a package didn't get done this round.
#[derive(Debug)]
enum Problem {
    /// Not its fault: try again after `after` (reported as "waiting", with `why`).
    Busy { after: Duration, why: String },
    /// It failed: `state` is "failed" or "remove_failed". Waits before the next try.
    Failed { state: &'static str, exit_code: Option<i64>, message: String },
}

fn failed(action: Action, exit_code: Option<i64>, message: impl Into<String>) -> Problem {
    let state = match action {
        Action::Install => "failed",
        Action::Remove => "remove_failed",
    };
    Problem::Failed { state, exit_code, message: message.into() }
}

/// The package got where it should be: its state, and whether it wants a restart.
struct Done {
    state: &'static str,
    restart: bool,
}

impl Done {
    fn state(state: &'static str) -> Self {
        Self { state, restart: false }
    }
}

/// What handling one package means for the round.
#[derive(Default)]
struct Handled {
    /// Come back sooner than the normal recheck.
    again: Option<Duration>,
    /// The computer should restart for it.
    restart: bool,
    /// Keep its downloaded installer (it will be tried again soon).
    keep_file: bool,
}

struct Worker {
    client: Arc<Client>,
    /// state_dir\software: downloaded installers and installation logs
    dir: PathBuf,
    local_path: PathBuf,
    local: Local,
    installer: Box<dyn Installer>,
    reporter: Reporter,
    /// This boot's start time (see local.rs, restart_pending)
    boot: Option<u64>,
}

impl Worker {
    fn new(client: Arc<Client>, state_dir: &Path, installer: Box<dyn Installer>) -> Self {
        let local_path = local::path_in(state_dir);
        Self {
            client,
            dir: state_dir.join("software"),
            local: Local::load(&local_path),
            local_path,
            installer,
            reporter: Reporter::default(),
            boot: None,
        }
    }

    fn run(mut self, configs: &Receiver<Option<SoftwareConfig>>) {
        let mut config: Option<SoftwareConfig> = None;
        let mut next_round: Option<Instant> = None;
        loop {
            let wait = next_round.map(|t| t.saturating_duration_since(Instant::now())).unwrap_or(RECHECK);
            let arrived = match configs.recv_timeout(wait) {
                Ok(first) => Some(newest(first, configs)),
                Err(RecvTimeoutError::Timeout) => None,
                Err(RecvTimeoutError::Disconnected) => return,
            };
            if let Some(latest) = arrived {
                // Every config change of the agent arrives here, mostly with our section
                // unchanged (e.g. a teacher opened the monitoring page): ignore those.
                if latest != config {
                    next_round = latest.as_ref().map(|c| Instant::now() + spread(c.spread_s));
                    log_new_list(latest.as_ref());
                    config = latest;
                }
                continue;
            }
            let Some(current) = config.clone() else {
                next_round = None;
                continue;
            };
            if next_round.is_some_and(|t| Instant::now() < t) {
                continue;
            }
            match self.round(&current, configs) {
                RoundEnd::Again(after) => next_round = Some(Instant::now() + after),
                RoundEnd::NewConfig(latest) => {
                    next_round = latest.as_ref().map(|c| Instant::now() + spread(c.spread_s));
                    log_new_list(latest.as_ref());
                    config = latest;
                }
                RoundEnd::Stopped => return,
            }
        }
    }

    /// Go through every package once.
    fn round(&mut self, config: &SoftwareConfig, configs: &Receiver<Option<SoftwareConfig>>) -> RoundEnd {
        if let Err(e) = std::fs::create_dir_all(self.dir.join("logs")) {
            log::warn!("software: can't create {}: {e}", self.dir.display());
        }
        self.boot = self.installer.boot_time();
        let (mut again, mut restart_for, mut keep) = (RECHECK, Vec::new(), HashSet::new());
        for p in &config.packages {
            // A new list while we were busy: start over with it (if it really changed).
            match configs.try_recv() {
                Ok(first) => {
                    let latest = newest(first, configs);
                    if latest.as_ref() != Some(config) {
                        self.save();
                        return RoundEnd::NewConfig(latest);
                    }
                }
                Err(TryRecvError::Empty) => {}
                Err(TryRecvError::Disconnected) => return RoundEnd::Stopped,
            }
            let handled = self.handle(p, &config.status_path);
            if let Some(after) = handled.again {
                again = again.min(after);
            }
            if handled.restart {
                restart_for.push(p.label());
            }
            if handled.keep_file {
                keep.insert(p.sha256.clone());
            }
            self.reporter.flush(&self.client, &config.status_path);
            self.save();
        }
        self.restart_if_needed(config, &restart_for);
        self.clean_up(config, &keep);
        self.save();
        RoundEnd::Again(again)
    }

    /// One package: do what's needed, remember and report how it went.
    fn handle(&mut self, p: &Package, status_path: &str) -> Handled {
        let recipe = recipe(p);
        if let Some(f) = self.local.failures.get(&p.id) {
            if f.recipe != recipe || f.state != failed_state(p.action) {
                self.local.failures.remove(&p.id); // the package changed: a new try at once
            } else if unix_now() < f.retry_at {
                let report = Report::new(&p.id, failed_state(p.action), &p.sha256).with(f.exit_code, &f.message);
                let again = Duration::from_secs(f.retry_at - unix_now());
                self.reporter.set(report);
                return Handled { again: Some(again), keep_file: true, ..Handled::default() };
            }
        }
        let result = match p.action {
            Action::Install => self.install(p, status_path),
            Action::Remove => self.remove(p, status_path),
        };
        match result {
            Ok(done) => {
                self.local.failures.remove(&p.id);
                self.reporter.set(Report::new(&p.id, done.state, &p.sha256));
                Handled { restart: done.restart, ..Handled::default() }
            }
            Err(Problem::Busy { after, why }) => {
                log::info!("software: {}: {why}; trying again in {} s", p.label(), after.as_secs());
                self.reporter.set(Report::new(&p.id, "waiting", &p.sha256).with(None, &why));
                Handled { again: Some(after), keep_file: true, ..Handled::default() }
            }
            Err(Problem::Failed { state, exit_code, message }) => {
                let attempts = self.local.failures.get(&p.id).map_or(0, |f| f.attempts) + 1;
                let pause = plan::retry_after_failure(attempts);
                log::warn!("software: {} {state}: {message} (attempt {attempts}; next in {} h)", p.label(), pause.as_secs() / 3600);
                self.local.failures.insert(p.id.clone(), Failure {
                    recipe,
                    state: state.into(),
                    exit_code,
                    message: message.clone(),
                    attempts,
                    retry_at: unix_now() + pause.as_secs(),
                });
                self.reporter.set(Report::new(&p.id, state, &p.sha256).with(exit_code, &message));
                Handled { again: Some(pause), keep_file: true, ..Handled::default() }
            }
        }
    }

    fn install(&mut self, p: &Package, status_path: &str) -> Result<Done, Problem> {
        let mut file = None;
        // An .msi rule without its own product code: the code comes from the file.
        let code = match &p.detection {
            Detection::Msi { .. } => Some(self.msi_code(p, &mut file, status_path)?),
            _ => None,
        };
        let installed = self.detect(p, code.as_deref())?;
        let ours = self.local.installed.get(&p.id).map(|i| i.sha256.clone());
        match plan::decide(Action::Install, installed, ours.as_deref(), &p.sha256) {
            Step::UpToDate => {
                let pending = self.local.restart_pending(&p.id, self.boot);
                return Ok(Done::state(if pending { "reboot_pending" } else { "installed" }));
            }
            Step::Adopt => {
                log::info!("software: {} is already installed; SMP manages it from now on", p.label());
                self.remember_installed(p, code, None);
                return Ok(Done::state("installed"));
            }
            Step::Install | Step::Remove | Step::Gone => {}
        }
        let file = match file {
            Some(f) => f,
            None => self.fetch(p, status_path)?,
        };
        self.progress(p, "installing", status_path);
        let log_file = self.log_path(p);
        log::info!("software: installing {}", p.label());
        let restart_needed = self.run_installer(p, &plan::install_invocation(p, &file, &log_file), &log_file)?;
        // Removal later needs an .msi's product code even if the rule is a registry/file one.
        let code = match (code, p.kind) {
            (None, Kind::Msi) => self.installer.msi_product_code(&file).ok(),
            (code, _) => code,
        };
        if !restart_needed && !self.detect(p, code.as_deref())? {
            return Err(failed(Action::Install, None,
                "the installer finished without an error, but the detection rule doesn't find the program: check the rule"));
        }
        let restart = plan::wants_restart(p, Action::Install, restart_needed);
        let pending_boot = if restart_needed || restart { self.boot } else { None };
        self.remember_installed(p, code, pending_boot);
        let _ = std::fs::remove_file(&file);
        log::info!("software: installed {}{}", p.label(), if pending_boot.is_some() { " (restart pending)" } else { "" });
        Ok(Done { state: if pending_boot.is_some() { "reboot_pending" } else { "installed" }, restart })
    }

    fn remove(&mut self, p: &Package, status_path: &str) -> Result<Done, Problem> {
        let mut file = None;
        let mut code = match &p.detection {
            Detection::Msi { .. } => Some(self.msi_code(p, &mut file, status_path)?),
            _ => None,
        };
        if !self.detect(p, code.as_deref())? {
            self.local.installed.remove(&p.id);
            return Ok(Done::state("removed"));
        }
        if code.is_none() && p.kind == Kind::Msi {
            code = Some(self.msi_code(p, &mut file, status_path)?); // msiexec /x needs it
        }
        self.progress(p, "removing", status_path);
        let log_file = self.log_path(p);
        let invocation = plan::uninstall_invocation(p, code.as_deref(), &log_file).map_err(|m| failed(Action::Remove, None, m))?;
        log::info!("software: removing {}", p.label());
        let restart_needed = self.run_installer(p, &invocation, &log_file)?;
        if !restart_needed && self.detect(p, code.as_deref())? {
            return Err(failed(Action::Remove, None,
                "the uninstaller finished without an error, but the detection rule still finds the program"));
        }
        self.local.installed.remove(&p.id);
        if let Some(f) = file {
            let _ = std::fs::remove_file(f);
        }
        log::info!("software: removed {}", p.label());
        Ok(Done { state: "removed", restart: plan::wants_restart(p, Action::Remove, restart_needed) })
    }

    fn remember_installed(&mut self, p: &Package, product_code: Option<String>, restart_pending_boot: Option<u64>) {
        self.local.installed.insert(p.id.clone(), Installed { sha256: p.sha256.clone(), product_code, restart_pending_boot });
    }

    /// An .msi's product code: known already, or read from the file (downloaded for it).
    fn msi_code(&mut self, p: &Package, file: &mut Option<PathBuf>, status_path: &str) -> Result<String, Problem> {
        if let Detection::Msi { product_code: Some(code) } = &p.detection {
            return Ok(code.clone());
        }
        if let Some(code) = self.local.known_product_code(&p.id, &p.sha256) {
            return Ok(code);
        }
        let f = self.fetch(p, status_path)?;
        let code = self.installer.msi_product_code(&f)
            .map_err(|e| failed(p.action, None, format!("couldn't read the product code from the .msi: {e:#}")))?;
        self.local.product_codes.insert(p.sha256.clone(), code.clone());
        *file = Some(f);
        Ok(code)
    }

    fn detect(&self, p: &Package, product_code: Option<&str>) -> Result<bool, Problem> {
        self.installer
            .is_installed(&p.detection, product_code)
            .map_err(|e| failed(p.action, None, format!("couldn't check whether it's installed: {e:#}")))
    }

    /// Run an installer or uninstaller; Ok(true): it needs a restart.
    fn run_installer(&self, p: &Package, invocation: &Invocation, log_file: &Path) -> Result<bool, Problem> {
        let see_log = if invocation.program == plan::MSIEXEC {
            format!(" (Windows Installer log on the computer: {})", log_file.display())
        } else {
            String::new()
        };
        match self.installer.run(p, invocation, Duration::from_secs(p.timeout_s)) {
            Err(e) => Err(failed(p.action, None, format!("couldn't start it: {e:#}"))),
            Ok(None) => Err(failed(p.action, None, format!("it didn't finish within {} minutes and was stopped{see_log}", p.timeout_s / 60))),
            Ok(Some(code)) => match plan::outcome(code, &p.success_codes) {
                Outcome::Done { restart_needed } => Ok(restart_needed),
                Outcome::Busy => Err(Problem::Busy { after: BUSY_RETRY, why: "another installation is running".into() }),
                Outcome::Failed(what) => Err(failed(p.action, Some(i64::from(code as u32)), format!("{what}{see_log}"))),
            },
        }
    }

    /// The package's installer, downloaded (or already here), checked against the
    /// size and SHA-256 SMP announced.
    fn fetch(&mut self, p: &Package, status_path: &str) -> Result<PathBuf, Problem> {
        let ready = self.dir.join(format!("{}.{}", p.sha256, p.kind.extension()));
        if std::fs::metadata(&ready).is_ok_and(|m| m.len() == p.size) {
            return Ok(ready); // checked when it was downloaded (the folder is SYSTEM/admins only)
        }
        self.progress(p, "downloading", status_path);
        let part = self.dir.join(format!("{}.part", p.sha256));
        log::info!("software: downloading {} ({} MB)", p.label(), p.size / 1024 / 1024);
        match self.client.download_resumable(&p.url, &part, p.size) {
            Ok((size, sha256)) if size == p.size && sha256 == p.sha256 => {
                std::fs::rename(&part, &ready)
                    .map_err(|e| failed(p.action, None, format!("couldn't store the installer: {e}")))?;
                Ok(ready)
            }
            Ok(_) => {
                let _ = std::fs::remove_file(&part);
                Err(Problem::Busy { after: DOWNLOAD_RETRY, why: "the download didn't match what SMP announced; downloading it again".into() })
            }
            Err(ApiError::Http { status: 503, retry_after, .. }) => Err(Problem::Busy {
                after: Duration::from_secs(retry_after.unwrap_or(60).clamp(5, 600)),
                why: "waiting for a download slot (other computers are downloading)".into(),
            }),
            Err(ApiError::Http { status: 416, .. }) => {
                let _ = std::fs::remove_file(&part); // our partial file is wrong: start over
                Err(Problem::Busy { after: Duration::from_secs(5), why: "restarting the download".into() })
            }
            Err(e) => Err(Problem::Busy { after: DOWNLOAD_RETRY, why: format!("the download stopped ({e}); it continues later") }),
        }
    }

    /// Report a step that takes a while right away (SMP shows it as it happens).
    fn progress(&mut self, p: &Package, state: &'static str, status_path: &str) {
        self.reporter.set(Report::new(&p.id, state, &p.sha256));
        self.reporter.flush(&self.client, status_path);
    }

    fn log_path(&self, p: &Package) -> PathBuf {
        self.dir.join("logs").join(format!("{}.log", p.id))
    }

    fn restart_if_needed(&mut self, config: &SoftwareConfig, programs: &[String]) {
        if programs.is_empty() {
            return;
        }
        if let (Some(boot), Some(asked)) = (self.boot, self.local.restart_requested_boot) {
            if local::same_boot(boot, asked) {
                return; // asked already in this boot (it's coming, or someone cancelled it)
            }
        }
        let after = if crate::platform::console_user().is_some() {
            Duration::from_secs(config.reboot_warning_s).max(RESTART_NOBODY_THERE)
        } else {
            RESTART_NOBODY_THERE
        };
        match self.installer.schedule_restart(after, programs) {
            Ok(()) => {
                log::info!("software: restarting in {} s for {}", after.as_secs(), programs.join(", "));
                self.local.restart_requested_boot = self.boot;
            }
            Err(e) => log::warn!("software: couldn't schedule the restart: {e:#}"),
        }
    }

    /// Delete installers we don't need any more (finished ones, packages gone from the
    /// list); keep those that will be tried again soon, and partial downloads of
    /// packages still on the list (they continue).
    fn clean_up(&self, config: &SoftwareConfig, keep: &HashSet<String>) {
        let listed: HashSet<&str> = config.packages.iter().map(|p| p.sha256.as_str()).collect();
        let Ok(entries) = std::fs::read_dir(&self.dir) else { return };
        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_file() {
                continue; // logs\
            }
            let name = entry.file_name().to_string_lossy().into_owned();
            let (sha, ext) = name.split_once('.').unwrap_or((&name, ""));
            let wanted = keep.contains(sha) || (ext == "part" && listed.contains(sha));
            if !wanted {
                let _ = std::fs::remove_file(&path);
            }
        }
    }

    fn save(&self) {
        if let Err(e) = self.local.save(&self.local_path) {
            log::warn!("software: couldn't save {}: {e:#}", self.local_path.display());
        }
    }
}

fn failed_state(action: Action) -> &'static str {
    match action {
        Action::Install => "failed",
        Action::Remove => "remove_failed",
    }
}

/// Everything about the package that decides how it installs: a change means a new try.
fn recipe(p: &Package) -> String {
    hex(&Sha256::digest(serde_json::to_vec(p).unwrap_or_default()))
}

/// The last of the configs waiting in the channel (only the newest matters).
fn newest(first: Option<SoftwareConfig>, configs: &Receiver<Option<SoftwareConfig>>) -> Option<SoftwareConfig> {
    let mut latest = first;
    while let Ok(next) = configs.try_recv() {
        latest = next;
    }
    latest
}

fn log_new_list(config: Option<&SoftwareConfig>) {
    match config {
        None => log::info!("software: nothing assigned to this computer"),
        Some(c) => log::info!("software: {} package(s) assigned; starting within {} s", c.packages.len(), c.spread_s),
    }
}

fn spread(max_s: u64) -> Duration {
    use rand_core::{OsRng, RngCore};
    Duration::from_secs(if max_s == 0 { 0 } else { OsRng.next_u64() % (max_s + 1) })
}

fn unix_now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::config::tests::{package, section};
    use super::fake::FakeInstaller;
    use super::*;
    use crate::core::identity::Identity;

    /// A worker whose server can't be reached (reports stay queued) and a fake installer.
    fn worker(dir: &Path) -> Worker {
        let client = Arc::new(Client::new("https://127.0.0.1:9", Identity::generate(), None));
        Worker::new(client, dir, Box::new(FakeInstaller::new(dir.join("fake.json"))))
    }

    fn registry_package(action: &str) -> Package {
        let mut v = package("Tool", action, "exe");
        v["detection"] = serde_json::json!({"type": "registry", "key": "HKLM\\SOFTWARE\\Tool"});
        v["uninstall_command"] = "\"C:\\Tool\\uninst.exe\" /S".into();
        serde_json::from_value(v).unwrap()
    }

    #[test]
    fn programs_already_there_are_taken_over_and_removed_on_request() {
        let dir = tempfile::tempdir().unwrap();
        let mut w = worker(dir.path());
        let install = registry_package("install");
        let fake = FakeInstaller::new(dir.path().join("fake.json"));
        fake.mark_installed(&install.detection);

        let handled = w.handle(&install, "/api/v1/software/agent/status");
        assert!(!handled.restart && handled.again.is_none());
        assert_eq!(w.local.installed[&install.id].sha256, install.sha256, "adopted");
        assert_eq!(w.reporter.queued().last().unwrap().state, "installed");

        let remove = registry_package("remove");
        w.handle(&remove, "/s");
        assert!(!w.local.installed.contains_key(&remove.id));
        let states: Vec<_> = w.reporter.queued().iter().map(|r| r.state).collect();
        assert_eq!(states, ["removed"], "the newest state per package");
        assert!(!fake.is_installed(&remove.detection, None).unwrap(), "the uninstaller ran");

        w.handle(&remove, "/s"); // and again: nothing left to do
        assert_eq!(w.reporter.queued().last().unwrap().state, "removed");
    }

    #[test]
    fn failures_wait_until_the_package_changes() {
        let dir = tempfile::tempdir().unwrap();
        let mut w = worker(dir.path());
        let mut remove = registry_package("remove");
        let fake = FakeInstaller::new(dir.path().join("fake.json"));
        fake.mark_installed(&remove.detection);
        fake.set_exit_code(1603);

        let handled = w.handle(&remove, "/s");
        assert_eq!(handled.again, Some(Duration::from_secs(3600)));
        let report = w.reporter.queued().last().unwrap().clone();
        assert_eq!((report.state, report.exit_code), ("remove_failed", Some(1603)));
        assert!(report.message.contains("fatal error"), "{}", report.message);

        fake.set_exit_code(0);
        w.handle(&remove, "/s");
        assert_eq!(w.local.failures[&remove.id].attempts, 1, "still waiting: not run again");
        assert!(fake.is_installed(&remove.detection, None).unwrap());

        remove.uninstall_command = "\"C:\\Tool\\uninst.exe\" /S /FORCE".into(); // the admin fixed it
        w.handle(&remove, "/s");
        assert!(!w.local.failures.contains_key(&remove.id));
        assert_eq!(w.reporter.queued().last().unwrap().state, "removed");
    }

    #[test]
    fn a_new_list_is_noticed_and_only_real_changes_count() {
        let a = SoftwareConfig::from_section(Some(&section(vec![package("A", "install", "msi")])));
        let b = SoftwareConfig::from_section(Some(&section(vec![package("B", "install", "msi")])));
        let (tx, rx) = mpsc::channel();
        tx.send(a.clone()).unwrap();
        tx.send(b.clone()).unwrap();
        assert_eq!(newest(None, &rx), b);
        assert_ne!(a, b);
        assert_eq!(a, SoftwareConfig::from_section(Some(&section(vec![package("A", "install", "msi")]))));
    }
}
