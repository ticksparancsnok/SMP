// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! The agent's main loop: join (wait for an admin's approval), then heartbeats.
//!
//! Heartbeats long-poll: the agent sends the config version it has, and the server
//! answers when something changes (or after ~25 s). So a teacher opening a room
//! reaches its PCs within a second, with one idle HTTPS request per PC in between.
//!
//! The other direction is quick too: while a heartbeat waits, the loop checks every
//! second whether someone logged on/off or the network address changed, and if so
//! reports it right away with a new heartbeat (the waiting one is left to finish on
//! its own thread; its answer is ignored).
//!
//! Getting back after the server was away: every error is retried (after
//! 1, 2, 4, ... up to 30 s), a heartbeat that doesn't answer in time counts as an error
//! instead of being waited for, network errors drop the pooled connections (they may be
//! dead), and the watchdog (watchdog.rs) restarts the process if nothing gets through
//! for 15 minutes anyway.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};

use crate::core::api::{ApiError, Client, HeartbeatIn, HeartbeatOut, AGENT_VERSION, PLATFORM};
use crate::core::capability::Registry;
use crate::core::identity::Identity;
use crate::core::state::State;
use crate::core::presence::{Presence, PresenceProbe};
use crate::core::sysinfo::SysInfo;
use crate::core::update;
use crate::core::watchdog::Contact;

/// Retries after errors wait 1, 2, 4, ... seconds, at most this long.
const MAX_RETRY_DELAY_S: u64 = 30;
/// How often a rejected computer checks whether it was allowed again (seconds).
const REJECTED_CHECK_EVERY_S: u64 = 600;
/// A heartbeat's answer is given up on after this (its own timeout is 40 s).
const ANSWER_DEADLINE: Duration = Duration::from_secs(60);

pub struct Agent {
    /// Shared with the capabilities (they upload too, e.g. screen frames).
    pub client: Arc<Client>,
    pub state: State,
    state_path: PathBuf,
    /// When the server last answered (the watchdog reads it).
    pub contact: Contact,
}

impl Agent {
    /// Load the saved identity, or (first run, `server` given) create a new one.
    pub fn open(state_path: &Path, server: Option<&str>) -> Result<Self> {
        let state = match State::load(state_path)? {
            Some(state) => {
                if let Some(server) = server {
                    if server.trim_end_matches('/') != state.server {
                        bail!(
                            "this computer already belongs to {}; to move it, delete {} first",
                            state.server,
                            state_path.display()
                        );
                    }
                }
                state
            }
            None => {
                let server = server.context("not set up yet: run `smp-agent join --server https://<smp address>` first")?;
                if !server.starts_with("https://") {
                    bail!("the server address must start with https://");
                }
                let state = State::new(server, &Identity::generate())?;
                state.save(state_path)?;
                log::info!("created this computer's identity key ({})", state_path.display());
                state
            }
        };
        let client = Arc::new(Client::new(&state.server, state.identity()?, state.server_cert_sha256.clone()));
        Ok(Self { client, state, state_path: state_path.to_path_buf(), contact: Contact::new() })
    }

    /// After a successful request: pin the server's certificate (first contact), or
    /// follow it when it changed to one this computer trusts (see tls.rs).
    fn remember_certificate(&mut self) {
        let seen = self.client.certificate_seen();
        if seen.is_some() && seen != self.state.server_cert_sha256 {
            if self.state.server_cert_sha256.is_none() {
                log::info!("pinned the server's certificate {}", seen.as_deref().unwrap_or(""));
            } else {
                log::info!("the server's certificate changed to a trusted one; pin updated");
            }
            self.state.server_cert_sha256 = seen.clone();
            self.client.pin_certificate(seen);
            if let Err(e) = self.state.save(&self.state_path) {
                log::warn!("couldn't save the certificate pin: {e:#}");
            }
        }
    }

    /// Where agent.json is: update.json goes next to it.
    pub fn state_dir(&self) -> PathBuf {
        self.state_path.parent().map(Path::to_path_buf).unwrap_or_default()
    }

    fn save_device_id(&mut self, device_id: &str) {
        if self.state.device_id.as_deref() != Some(device_id) {
            self.state.device_id = Some(device_id.to_string());
            if let Err(e) = self.state.save(&self.state_path) {
                log::warn!("couldn't save the device id: {e:#}");
            }
        }
    }

    /// Ask to be managed and wait until an admin approves (or `stop` is set).
    /// `on_pending` is called with the fingerprint to show while waiting.
    pub fn wait_until_approved(&mut self, stop: &AtomicBool, mut on_pending: impl FnMut(&str)) -> Result<bool> {
        let mut asked = false;
        let mut delay = 5;
        while !stop.load(Ordering::Relaxed) {
            let status = self.client.join_status();
            if status.is_ok() {
                self.contact.touch();
            }
            match status {
                Ok(s) if s.status == "approved" => {
                    self.remember_certificate();
                    return Ok(true);
                }
                Ok(s) if s.status == "pending" => {
                    self.remember_certificate();
                    if !asked {
                        on_pending(&s.fingerprint_display);
                        asked = true;
                    }
                    delay = 5;
                }
                Ok(s) if s.status == "rejected" => {
                    // An admin said no. Asking again wouldn't change it (the server
                    // remembers this key and MAC); check now and then whether someone
                    // chose "allow again" in SMP.
                    if delay != REJECTED_CHECK_EVERY_S {
                        log::warn!("an admin rejected this computer in SMP (Devices); waiting");
                    }
                    delay = REJECTED_CHECK_EVERY_S;
                }
                Ok(_) | Err(ApiError::Unauthorized) => {
                    // Never asked, expired, forgotten or reset: (re)send the join request.
                    match self.client.join(&SysInfo::collect()) {
                        Ok(r) if r.status == "rejected" => {
                            // e.g. reinstalled with a new key, but this machine was rejected
                            self.remember_certificate();
                            log::warn!("an admin rejected this computer in SMP (Devices); waiting");
                            delay = REJECTED_CHECK_EVERY_S;
                        }
                        Ok(r) => {
                            self.remember_certificate();
                            on_pending(&r.fingerprint_display);
                            asked = true;
                            delay = 5;
                            if r.status == "approved" {
                                return Ok(true);
                            }
                        }
                        Err(e) => log::warn!("join request failed: {e}"),
                    }
                }
                Err(e) => {
                    log::warn!("{e}");
                    if matches!(e, ApiError::Transport(_)) {
                        self.client.reset_connections();
                    }
                    delay = (delay * 2).min(MAX_RETRY_DELAY_S);
                }
            }
            sleep_unless(stop, delay);
        }
        Ok(false)
    }

    /// Heartbeats until `stop` is set; capabilities get every config change.
    pub fn run(&mut self, registry: &mut Registry, stop: &AtomicBool) -> Result<()> {
        log::info!("smp-agent {AGENT_VERSION}, capabilities: {:?}", registry.names());
        let mut version: Option<String> = None; // the config we're running with
        let mut approved = false; // checked with the server since this run started
        let mut delay = 1; // retry delay after errors, doubles up to MAX_RETRY_DELAY_S
        let mut confirmed = false; // a pending update to this version is confirmed once per run
        let mut probe = PresenceProbe::new(&self.state.server);
        // Heartbeats run on their own threads and send (number, answer) back here.
        let (answers_tx, answers) = mpsc::channel::<(u64, Result<HeartbeatOut, ApiError>)>();
        let mut sent = 0u64;
        while !stop.load(Ordering::Relaxed) {
            if !approved {
                let shown = |fp: &str| {
                    log::warn!("waiting for an admin to approve this computer in SMP (Devices). Fingerprint: {fp}")
                };
                if !self.wait_until_approved(stop, shown)? {
                    break; // stopped while waiting
                }
                approved = true;
                log::info!("approved (fingerprint {})", self.client.identity().display_fingerprint());
            }
            let presence = probe.sample();
            let info = SysInfo::collect();
            let request = HeartbeatIn {
                hostname: info.hostname,
                agent_version: AGENT_VERSION.to_string(),
                os_name: info.os_name,
                os_version: info.os_version,
                user: presence.user.clone(),
                config_version: version.clone(),
                wait: version.is_some(),
                platform: PLATFORM,
                address: presence.address.map(|a| a.to_string()),
                capabilities: registry.names(),
            };
            sent += 1;
            let (client, answers_tx, number) = (self.client.clone(), answers_tx.clone(), sent);
            std::thread::spawn(move || {
                let _ = answers_tx.send((number, client.heartbeat(&request)));
            });
            let Some(answer) = wait_for_answer(&self.client, &answers, sent, stop, &mut probe, &presence) else {
                continue; // stopping, or something changed: report it now
            };
            match answer {
                Ok(resp) => {
                    self.contact.touch();
                    self.remember_certificate();
                    self.save_device_id(&resp.device_id);
                    if !confirmed {
                        update::confirm(&self.state_dir()); // we got through: an update to us worked
                        confirmed = true;
                    }
                    if version.as_deref() != Some(resp.config_version.as_str()) {
                        log::info!("config {}: {}", resp.config_version, resp.config);
                        registry.apply(&resp.config);
                        version = Some(resp.config_version);
                    }
                    delay = 1;
                }
                Err(ApiError::Unauthorized) => {
                    log::warn!("the server no longer accepts this computer (removed or identity reset); asking to join again");
                    registry.apply(&serde_json::json!({})); // stop every capability
                    version = None;
                    approved = false;
                }
                Err(e) => {
                    log::warn!("{e}; retrying in {delay} s");
                    if matches!(e, ApiError::Transport(_)) {
                        // pooled connections may be dead (server restarted, network changed)
                        self.client.reset_connections();
                    }
                    // ...or as soon as the network comes back with a (new) address
                    sleep_until_change(&self.client, stop, delay, &mut probe, &presence);
                    delay = (delay * 2).min(MAX_RETRY_DELAY_S);
                }
            }
        }
        registry.stop();
        Ok(())
    }
}

/// The answer to heartbeat number `number`, or None when we should stop or when the
/// user / network address changed meanwhile (then a new heartbeat reports it). A
/// heartbeat that hasn't answered within ANSWER_DEADLINE is an error: its thread may
/// hang on something the HTTP timeout doesn't cover (e.g. a name lookup), and waiting
/// for it forever is how an agent would never reconnect.
fn wait_for_answer(
    client: &Client,
    answers: &Receiver<(u64, Result<HeartbeatOut, ApiError>)>,
    number: u64,
    stop: &AtomicBool,
    probe: &mut PresenceProbe,
    presence: &Presence,
) -> Option<Result<HeartbeatOut, ApiError>> {
    let started = Instant::now();
    loop {
        match answers.recv_timeout(Duration::from_secs(1)) {
            Ok((n, answer)) if n == number => return Some(answer),
            Ok(_) => {} // a heartbeat we stopped waiting for finished
            Err(_) => {
                if stop.load(Ordering::Relaxed) {
                    return None;
                }
                if started.elapsed() > ANSWER_DEADLINE {
                    return Some(Err(ApiError::Transport(format!(
                        "no answer within {} s", ANSWER_DEADLINE.as_secs()
                    ))));
                }
                let now = probe.sample();
                if now != *presence {
                    log::info!("{}; reporting it now", now.describe_change(presence));
                    if now.address != presence.address {
                        client.reset_connections(); // the old connections went with the old address
                    }
                    return None;
                }
            }
        }
    }
}

/// Sleep up to `seconds`, waking early on `stop` or when the user/address changes.
fn sleep_until_change(client: &Client, stop: &AtomicBool, seconds: u64, probe: &mut PresenceProbe, presence: &Presence) {
    for _ in 0..seconds {
        sleep_unless(stop, 1);
        if stop.load(Ordering::Relaxed) {
            return;
        }
        let now = probe.sample();
        if now != *presence {
            if now.address != presence.address {
                client.reset_connections();
            }
            return;
        }
    }
}

/// Sleep `seconds`, waking up early when `stop` is set.
pub fn sleep_unless(stop: &AtomicBool, seconds: u64) {
    for _ in 0..seconds * 10 {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}
