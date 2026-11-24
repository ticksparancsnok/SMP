// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Screen lock: a teacher locks a room's computers (or some of them) from the
//! Monitoring page. They then show a full-screen message with the school's logo, and
//! keyboard and mouse do nothing, until the teacher unlocks them.
//!
//! The server's "classroom" module sends it inside its config section, next to the
//! screen monitoring settings:
//!
//!   "classroom": {"active": false,
//!                 "lock": {"message": "Eyes to the front, please!", "language": "en",
//!                          "logo": {"sha256": "...", "size": 48213,
//!                                   "url": "/api/v1/classroom/agent/lock-logo"}}}
//!
//! No "lock" key: not locked. "logo": null: the school has no logo of its own, the
//! SMP logo built into the agent is shown. The server always fills in the message (the
//! teacher's, the school's suggestion, or a built-in sentence); should it ever be
//! empty, the agent shows its own sentence in `language`: the school's block page
//! language ("hu" | "en"), never the PC's Windows language, so one SMP setting decides
//! what every student reads.
//!
//! The logo is downloaded once (a signed request, like every other one) and kept in
//! memory by its SHA-256; the config carries only its fingerprint, since heartbeats
//! repeat the config every half minute.
//!
//! Fail-open, on purpose: the lock is a classroom tool, not a security boundary.
//! Whenever the agent stops (service stopped, crashed, restarted by the watchdog after
//! 15 minutes without the server, uninstalled), the lock goes with it; a school's PCs
//! can never stay locked because SMP is away. See platform/win/lock_window.rs for what
//! the lock does and doesn't stop on Windows (Ctrl+Alt+Del always works).
//!
//! Threads: like the screen capability, `apply_config` only hands the config to a
//! worker thread, which downloads the logo and talks to the platform.

pub mod picture;

use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;

use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::core::api::Client;
use crate::core::capability::Capability;
use crate::core::identity::hex;

/// The longest message shown (characters); the server allows less.
const MAX_MESSAGE_CHARS: usize = 1000;
/// The largest logo file accepted (SMP accepts at most 1 MB).
const MAX_LOGO_BYTES: u64 = 2 * 1024 * 1024;
/// A lock without a text of its own shows this, in the school's language (the server
/// has the same sentences: LOCK_DEFAULT_TEXT in server/smp/modules/classroom/__init__.py).
fn default_text(language: &str) -> &'static str {
    match language {
        "en" => "This computer was locked by the teacher.",
        _ => "A számítógépet a tanár zárolta.", // "hu", and SMP's default language
    }
}

/// A logo that couldn't be downloaded is tried again after this (the built-in one is
/// shown meanwhile: the lock itself never waits for the logo).
const LOGO_RETRY: Duration = Duration::from_secs(60);

/// What a locked screen shows.
#[derive(Debug, Clone, PartialEq)]
pub struct LockScreen {
    /// The text to show (never empty: see LockConfig::from_section).
    pub message: String,
    /// The school's logo (PNG); None: the SMP logo built into the agent.
    pub logo: Option<Arc<Vec<u8>>>,
}

impl LockScreen {
    /// The PNG to show: the school's logo, or SMP's. (Windows sends SMP's as "none"
    /// and the helper fills it in, so only the other platforms ask here.)
    #[cfg_attr(windows, allow(dead_code))]
    pub fn logo_png(&self) -> &[u8] {
        self.logo.as_deref().map(Vec::as_slice).unwrap_or(picture::SMP_LOGO)
    }
}

/// Where locking happens. The capability decides *what* to show; the platform how:
///
///   Windows   the session helper (in the user's session) opens a full-screen window
///             on every monitor and blocks keyboard and mouse (platform/win/)
///   Linux     a log line, and the fake screen of test PCs shows the lock picture
pub trait LockTarget: Send {
    /// Lock with `lock` (called again when the message or the logo changes), or
    /// unlock (None).
    fn set_lock(&mut self, lock: Option<&LockScreen>);
}

/// The school's logo as the config describes it.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct LogoRef {
    pub sha256: String,
    pub size: u64,
    pub url: String,
}

/// The "lock" entry of the classroom section.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct LockConfig {
    #[serde(default)]
    pub message: String,
    /// The school's language for locked screens ("hu" | "en"; SMP's block page language).
    #[serde(default)]
    pub language: String,
    #[serde(default)]
    pub logo: Option<LogoRef>,
}

impl LockConfig {
    /// From the "classroom" section; None = not locked. Something unreadable also
    /// counts as "not locked" (fail-open, see the top of this file).
    pub fn from_section(section: Option<&Value>) -> Option<Self> {
        let lock = section?.get("lock")?;
        if lock.is_null() {
            return None;
        }
        let mut config: Self = match serde_json::from_value(lock.clone()) {
            Ok(c) => c,
            Err(e) => {
                log::warn!("ignoring a lock the agent can't read ({e}): {lock}");
                return None;
            }
        };
        config.message = clean_message(&config.message);
        if config.message.is_empty() {
            config.message = default_text(&config.language).to_string();
        }
        // Only from our own API, only a real SHA-256, only a sane size.
        let usable = |l: &LogoRef| {
            l.url.starts_with("/api/")
                && l.sha256.len() == 64
                && l.sha256.bytes().all(|b| b.is_ascii_hexdigit())
                && (1..=MAX_LOGO_BYTES).contains(&l.size)
        };
        if config.logo.as_ref().is_some_and(|l| !usable(l)) {
            log::warn!("ignoring the lock screen logo the server described: {:?}", config.logo);
            config.logo = None;
        }
        Some(config)
    }
}

/// At most MAX_MESSAGE_CHARS, line breaks as "\n", no other control characters.
fn clean_message(text: &str) -> String {
    text.replace("\r\n", "\n")
        .chars()
        .filter(|&c| c == '\n' || !c.is_control())
        .take(MAX_MESSAGE_CHARS)
        .collect::<String>()
        .trim()
        .to_string()
}

enum Message {
    Config(Option<LockConfig>),
    Stop,
}

pub struct Lock {
    to_worker: Sender<Message>,
    worker: Option<JoinHandle<()>>,
}

impl Lock {
    pub fn new(client: Arc<Client>, target: Box<dyn LockTarget>) -> Self {
        let (to_worker, inbox) = mpsc::channel();
        let worker = std::thread::Builder::new()
            .name("lock".into())
            .spawn(move || Worker::new(target, move |r: &LogoRef| download_logo(&client, r)).run(inbox))
            .expect("starting the lock thread");
        Self { to_worker, worker: Some(worker) }
    }
}

impl Capability for Lock {
    fn name(&self) -> &'static str {
        "lock"
    }

    /// The server's classroom module sends the lock (see the top of this file).
    fn section(&self) -> &'static str {
        "classroom"
    }

    fn apply_config(&mut self, section: Option<&Value>) {
        let _ = self.to_worker.send(Message::Config(LockConfig::from_section(section)));
    }

    fn stop(&mut self) {
        let _ = self.to_worker.send(Message::Stop);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

/// Fetch the logo and check it is the one the config describes.
fn download_logo(client: &Client, logo: &LogoRef) -> Result<Vec<u8>, String> {
    let bytes = client.get_bytes(&logo.url, logo.size.min(MAX_LOGO_BYTES)).map_err(|e| e.to_string())?;
    let sha256 = hex(&Sha256::digest(&bytes));
    if bytes.len() as u64 != logo.size || !sha256.eq_ignore_ascii_case(&logo.sha256) {
        return Err(format!("the logo doesn't match its description (size {}, SHA-256 {sha256})", bytes.len()));
    }
    Ok(bytes)
}

/// The worker's state. `fetch` is how a logo is downloaded: a closure, so the tests
/// can hand in a fake instead of a server.
struct Worker<F> {
    target: Box<dyn LockTarget>,
    fetch: F,
    /// What the server wants (None: unlocked).
    wanted: Option<LockConfig>,
    /// What the platform shows now.
    shown: Option<LockScreen>,
    /// The last logo downloaded: (SHA-256, PNG).
    logo: Option<(String, Arc<Vec<u8>>)>,
    /// The last logo download failed: try again later, not at every config.
    logo_error: Option<String>,
}

impl<F: FnMut(&LogoRef) -> Result<Vec<u8>, String>> Worker<F> {
    fn new(target: Box<dyn LockTarget>, fetch: F) -> Self {
        Self { target, fetch, wanted: None, shown: None, logo: None, logo_error: None }
    }

    fn run(mut self, inbox: Receiver<Message>) {
        loop {
            // While a logo is missing, wake up now and then to try it again.
            let message = if self.logo_missing() {
                inbox.recv_timeout(LOGO_RETRY)
            } else {
                inbox.recv().map_err(|_| RecvTimeoutError::Disconnected)
            };
            match message {
                Ok(Message::Config(config)) => self.wanted = config,
                Ok(Message::Stop) | Err(RecvTimeoutError::Disconnected) => break,
                Err(RecvTimeoutError::Timeout) => {}
            }
            self.update();
        }
        if self.shown.is_some() {
            log::info!("the agent stops: unlocking the screen");
            self.target.set_lock(None); // never leave a computer locked behind us
        }
    }

    fn logo_missing(&self) -> bool {
        let wanted = self.wanted.as_ref().and_then(|w| w.logo.as_ref());
        wanted.is_some_and(|w| self.logo.as_ref().is_none_or(|(sha, _)| !sha.eq_ignore_ascii_case(&w.sha256)))
    }

    /// Bring the platform in line with `wanted`.
    fn update(&mut self) {
        if self.logo_missing() {
            let wanted = self.wanted.as_ref().and_then(|w| w.logo.clone()).expect("checked by logo_missing");
            match (self.fetch)(&wanted) {
                Ok(png) => {
                    log::info!("lock screen logo downloaded ({} bytes)", png.len());
                    self.logo = Some((wanted.sha256.clone(), Arc::new(png)));
                    self.logo_error = None;
                }
                Err(e) => {
                    if self.logo_error.as_ref() != Some(&e) {
                        log::warn!("couldn't get the lock screen logo: {e}; showing SMP's meanwhile");
                    }
                    self.logo_error = Some(e);
                }
            }
        }
        let have_logo = !self.logo_missing();
        let screen = self.wanted.as_ref().map(|w| LockScreen {
            message: w.message.clone(),
            // the school's logo once we have it; SMP's until then, and when it has none
            logo: match (&w.logo, &self.logo) {
                (Some(_), Some((_, png))) if have_logo => Some(png.clone()),
                _ => None,
            },
        });
        if screen != self.shown {
            match (&screen, &self.shown) {
                (Some(s), None) => log::info!("screen locked: {:?}", s.message),
                (Some(s), Some(_)) => log::info!("lock screen changed: {:?}", s.message),
                (None, _) => log::info!("screen unlocked"),
            }
            self.target.set_lock(screen.as_ref());
            self.shown = screen;
        }
    }
}

/// For platforms without a real lock: the log says what would happen, and test PCs'
/// fake screens (SMP_AGENT_FAKE_SCREEN) show the lock picture through `shared`.
#[cfg_attr(windows, allow(dead_code))]
pub struct LogLock {
    pub shared: Arc<std::sync::Mutex<Option<LockScreen>>>,
}

impl LockTarget for LogLock {
    fn set_lock(&mut self, lock: Option<&LockScreen>) {
        match lock {
            Some(l) => log::info!("LOCKED (no real lock on this platform): {:?}, logo {} bytes", l.message, l.logo_png().len()),
            None => log::info!("UNLOCKED"),
        }
        *self.shared.lock().unwrap() = lock.cloned();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::Mutex;

    #[test]
    fn config_from_the_server() {
        assert_eq!(LockConfig::from_section(None), None);
        assert_eq!(LockConfig::from_section(Some(&json!({"active": false}))), None);
        assert_eq!(LockConfig::from_section(Some(&json!({"active": false, "lock": null}))), None);
        assert_eq!(LockConfig::from_section(Some(&json!({"lock": "garbage"}))), None, "unreadable = unlocked");

        let lock = LockConfig::from_section(Some(&json!({"lock": {"message": "  Figyelj!\r\nKöszi\u{7}  "}}))).unwrap();
        assert_eq!(lock.message, "Figyelj!\nKöszi");
        assert_eq!(lock.logo, None);

        // no text: the school's language decides the built-in one (not Windows')
        let text = |lock: serde_json::Value| LockConfig::from_section(Some(&json!({"lock": lock}))).unwrap().message;
        assert_eq!(text(json!({"message": "", "language": "en"})), "This computer was locked by the teacher.");
        assert_eq!(text(json!({"message": " ", "language": "hu"})), "A számítógépet a tanár zárolta.");
        assert_eq!(text(json!({})), "A számítógépet a tanár zárolta.", "SMP's default language");

        let logo = json!({"sha256": "ab".repeat(32), "size": 1000, "url": "/api/v1/classroom/agent/lock-logo"});
        let lock = LockConfig::from_section(Some(&json!({"lock": {"message": "", "logo": logo}}))).unwrap();
        assert_eq!(lock.logo.unwrap().size, 1000);

        for bad in [
            json!({"sha256": "ab".repeat(32), "size": 1000, "url": "https://elsewhere.example/logo.png"}),
            json!({"sha256": "short", "size": 1000, "url": "/api/x"}),
            json!({"sha256": "ab".repeat(32), "size": 999_999_999, "url": "/api/x"}),
        ] {
            let lock = LockConfig::from_section(Some(&json!({"lock": {"message": "x", "logo": bad}}))).unwrap();
            assert_eq!(lock.logo, None, "still locked, but with the built-in logo");
        }
        let long = "é".repeat(5000);
        assert_eq!(LockConfig::from_section(Some(&json!({"lock": {"message": long}}))).unwrap().message.chars().count(), 1000);
    }

    /// Records what the platform was asked to show.
    struct Recorder(Arc<Mutex<Vec<Option<LockScreen>>>>);
    impl LockTarget for Recorder {
        fn set_lock(&mut self, lock: Option<&LockScreen>) {
            self.0.lock().unwrap().push(lock.cloned());
        }
    }

    fn lock_with_logo(sha: &str) -> Option<LockConfig> {
        Some(LockConfig {
            message: "Hi".into(),
            language: "en".into(),
            logo: Some(LogoRef { sha256: sha.into(), size: 3, url: "/api/logo".into() }),
        })
    }

    #[test]
    fn locks_at_once_and_adds_the_logo_when_it_arrives() {
        let shown = Arc::new(Mutex::new(vec![]));
        let fetches = Arc::new(Mutex::new(0)); // how often the logo was downloaded
        let server_up = Arc::new(Mutex::new(false));
        let fake_download = {
            let (fetches, server_up) = (fetches.clone(), server_up.clone());
            move |_: &LogoRef| {
                *fetches.lock().unwrap() += 1;
                if *server_up.lock().unwrap() { Ok(b"png".to_vec()) } else { Err("server busy".to_string()) }
            }
        };
        let mut w = Worker::new(Box::new(Recorder(shown.clone())), fake_download);

        w.wanted = lock_with_logo("aa");
        w.update(); // the download fails: locked anyway, with SMP's logo
        assert_eq!(shown.lock().unwrap().last().unwrap().as_ref().unwrap().logo, None);
        assert!(w.logo_missing());

        *server_up.lock().unwrap() = true;
        w.update(); // the retry (LOGO_RETRY later in real life) works
        let last = shown.lock().unwrap().last().unwrap().clone().unwrap();
        assert_eq!(last.logo.as_deref().map(Vec::as_slice), Some(&b"png"[..]));

        w.update(); // nothing changed: not shown again, not downloaded again
        assert_eq!((shown.lock().unwrap().len(), *fetches.lock().unwrap()), (2, 2));

        w.wanted = lock_with_logo("bb"); // a new logo
        w.update();
        assert_eq!(*fetches.lock().unwrap(), 3);

        w.wanted = None;
        w.update();
        assert_eq!(shown.lock().unwrap().last().unwrap(), &None);
    }

    #[test]
    fn stopping_unlocks() {
        let shown = Arc::new(Mutex::new(vec![]));
        let (tx, rx) = mpsc::channel();
        let w = Worker::new(Box::new(Recorder(shown.clone())), |_: &LogoRef| Err("unused".to_string()));
        tx.send(Message::Config(Some(LockConfig { message: "x".into(), language: "hu".into(), logo: None }))).unwrap();
        tx.send(Message::Stop).unwrap();
        w.run(rx);
        let shown = shown.lock().unwrap();
        assert!(shown[0].is_some() && shown[1].is_none(), "{shown:?}");
    }
}
