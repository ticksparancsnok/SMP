// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Remote control: a teacher sees this computer's screen live and uses its mouse and
//! keyboard, from the Monitoring page.
//!
//!   teacher's browser <-- WebSocket --> SMP <-- signed HTTP --> this agent
//!
//! The server's "classroom" section carries, while a teacher controls this computer:
//!
//!   "remote": {"session": "9f3c...", "interval_ms": 200, "width": 1600, "quality": 60,
//!              "frame_path": "/api/v1/classroom/agent/remote/frame",
//!              "input_path": "/api/v1/classroom/agent/remote/input"}
//!
//! and nothing otherwise. Two threads do the work while it's there:
//!
//!   frames   every interval_ms: capture the screen with the mouse pointer, and upload
//!            it if it changed (unchanged screens cost nothing but a note every 2 s)
//!   input    asks the server for the teacher's input (a long poll: the answer comes as
//!            soon as there is some), and replays it; when the session ends, lets go of
//!            any key or button still held
//!
//! They are separate so a slow upload never delays a click, and a waiting input
//! request never delays a picture. The user is told by the tray icon (a third state:
//! "a teacher is controlling this computer"). Old agents ignore the "remote" key; SMP
//! only offers control to agents that report this capability.
//!
//! "block_local": true when the teacher blocked the student's own keyboard and mouse;
//! then only the teacher's input gets through (platform/win/input_block.rs explains
//! how it's told apart). Fail-safe: if this agent can't reach SMP for BLOCK_GRACE (the
//! teacher can't control it then either), it lets the student's input through again,
//! and blocks again once SMP answers, if the session is still on.
//!
//! Not (yet) possible: Windows' secure screens (UAC prompts, Ctrl+Alt+Del, the lock
//! screen) can't be captured or controlled from the user's desktop; the teacher then
//! sees the last picture.

pub mod input;

use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use anyhow::Result;
use serde::Deserialize;
use serde_json::Value;

use crate::capabilities::screen::encode::Picture;
use crate::core::api::{ApiError, Client, FrameUpload};
use crate::core::capability::Capability;
use input::{InputEvent, Pressed};

/// Even an unchanged screen is confirmed this often (the teacher sees it's still live).
const UNCHANGED_NOTE_EVERY: Duration = Duration::from_secs(2);
/// An input request may wait this long; the server answers within 20 s.
const INPUT_TIMEOUT: Duration = Duration::from_secs(30);
/// How long the student's input stays blocked without contact to SMP.
const BLOCK_GRACE: Duration = Duration::from_secs(30);

/// The screen, as remote control needs it.
pub trait RemoteScreen: Send {
    /// The primary screen *with the mouse pointer*, at most `max_width` wide, as JPEG.
    fn capture(&mut self, max_width: u32, quality: u8) -> Result<Picture>;
    /// Show the user that a teacher controls this computer (Windows: the tray icon),
    /// and whether only the teacher's input gets through (`block_local`).
    fn set_control(&mut self, controlled: bool, block_local: bool);
}

/// Where the teacher's input goes (Windows: SendInput in the user's session).
pub trait InputSink: Send {
    fn inject(&mut self, events: &[InputEvent]);
}

/// What the server asks for. Numbers are clamped, paths must be our own API.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RemoteConfig {
    pub session: String,
    #[serde(default = "default_interval")]
    pub interval_ms: u64,
    #[serde(default = "default_width")]
    pub width: u32,
    #[serde(default = "default_quality")]
    pub quality: u8,
    /// The student's own keyboard and mouse blocked (the teacher's choice).
    #[serde(default)]
    pub block_local: bool,
    pub frame_path: String,
    pub input_path: String,
}

fn default_interval() -> u64 {
    200
}
fn default_width() -> u32 {
    1600
}
fn default_quality() -> u8 {
    60
}

impl RemoteConfig {
    /// From the "classroom" section; None = nobody controls this computer.
    pub fn from_section(section: Option<&Value>) -> Option<Self> {
        let mut c: Self = serde_json::from_value(section?.get("remote")?.clone()).ok()?;
        let session_ok = !c.session.is_empty() && c.session.len() <= 64 && c.session.bytes().all(|b| b.is_ascii_alphanumeric());
        let paths_ok = [&c.frame_path, &c.input_path].iter().all(|p| p.starts_with("/api/") && !p.contains('?'));
        if !session_ok || !paths_ok {
            log::warn!("ignoring a remote control request the agent can't use: {c:?}");
            return None;
        }
        c.interval_ms = c.interval_ms.clamp(50, 5000);
        c.width = c.width.clamp(640, 3840);
        c.quality = c.quality.clamp(20, 95);
        Some(c)
    }
}

pub struct Remote {
    to_frames: Sender<Option<RemoteConfig>>,
    to_input: Sender<Option<RemoteConfig>>,
    frames: Option<JoinHandle<()>>,
}

impl Remote {
    pub fn new(client: Arc<Client>, screen: Box<dyn RemoteScreen>, sink: Box<dyn InputSink>) -> Self {
        let (to_frames, frames_inbox) = mpsc::channel();
        let (to_input, input_inbox) = mpsc::channel();
        let for_frames = client.clone();
        let frames = std::thread::Builder::new()
            .name("remote-frames".into())
            .spawn(move || FrameWorker::new(for_frames, screen).run(frames_inbox))
            .expect("starting the remote frames thread");
        // The input thread may sit in a long poll for up to 30 s, so it isn't joined at
        // shutdown (the frames thread is); it ends with the process.
        std::thread::Builder::new()
            .name("remote-input".into())
            .spawn(move || input_loop(&client, sink, &input_inbox))
            .expect("starting the remote input thread");
        Self { to_frames, to_input, frames: Some(frames) }
    }
}

impl Capability for Remote {
    fn name(&self) -> &'static str {
        "remote"
    }

    /// The server's classroom module sends it (see the top of this file).
    fn section(&self) -> &'static str {
        "classroom"
    }

    fn apply_config(&mut self, section: Option<&Value>) {
        let config = RemoteConfig::from_section(section);
        let _ = self.to_frames.send(config.clone());
        let _ = self.to_input.send(config);
    }

    fn stop(&mut self) {
        let _ = self.to_frames.send(None); // stop sending, tray icon back to normal
        let (closed, _) = mpsc::channel();
        self.to_frames = closed; // dropping the old sender ends the thread's loop
        if let Some(frames) = self.frames.take() {
            let _ = frames.join();
        }
    }
}

// ------------------------------------------------------------------------- frames

struct FrameWorker {
    client: Arc<Client>,
    screen: Box<dyn RemoteScreen>,
    config: Option<RemoteConfig>,
    /// When the next picture is due.
    next: Instant,
    /// Hash of the last picture the server got (None: send the next one in full).
    sent_hash: Option<u64>,
    last_upload: Instant,
    /// The last problem, logged once (not five times a second).
    last_error: Option<String>,
    /// The last time SMP answered an upload (BLOCK_GRACE, see the top of this file).
    last_contact: Instant,
    /// What the platform was told: (controlled, student's input blocked).
    applied: (bool, bool),
}

impl FrameWorker {
    fn new(client: Arc<Client>, screen: Box<dyn RemoteScreen>) -> Self {
        let now = Instant::now();
        Self { client, screen, config: None, next: now, sent_hash: None, last_upload: now, last_error: None,
               last_contact: now, applied: (false, false) }
    }

    /// Tell the platform what should be in effect now, if that changed.
    fn apply(&mut self) {
        let controlled = self.config.is_some();
        let blocked = self.config.as_ref().is_some_and(|c| c.block_local) && self.last_contact.elapsed() < BLOCK_GRACE;
        if (controlled, blocked) != self.applied {
            if controlled && self.applied.1 != blocked {
                let why = if !blocked && self.config.as_ref().is_some_and(|c| c.block_local) { " (no contact to SMP)" } else { "" };
                log::info!("student's keyboard and mouse {}{why}", if blocked { "blocked by the teacher" } else { "work again" });
            }
            self.screen.set_control(controlled, blocked);
            self.applied = (controlled, blocked);
        }
    }

    fn run(mut self, inbox: Receiver<Option<RemoteConfig>>) {
        loop {
            let message = match &self.config {
                Some(_) => inbox.recv_timeout(self.next.saturating_duration_since(Instant::now())),
                None => inbox.recv().map_err(|_| RecvTimeoutError::Disconnected),
            };
            match message {
                Ok(config) => self.reconfigure(config),
                Err(RecvTimeoutError::Timeout) => self.send_picture(),
                Err(RecvTimeoutError::Disconnected) => break,
            }
            self.apply();
        }
        self.reconfigure(None);
        self.apply();
    }

    fn reconfigure(&mut self, config: Option<RemoteConfig>) {
        let (was, is) = (self.config.as_ref().map(|c| c.session.clone()), config.as_ref().map(|c| c.session.clone()));
        if was != is {
            match &is {
                Some(_) => log::info!("remote control started: a teacher sees and controls this screen"),
                None if was.is_some() => log::info!("remote control ended"),
                None => {}
            }
            self.sent_hash = None; // a new session starts with a full picture, at once
            self.next = Instant::now();
            self.last_contact = Instant::now(); // a fresh session counts as contact
        }
        self.config = config;
    }

    fn send_picture(&mut self) {
        let Some(c) = self.config.clone() else { return };
        self.next = Instant::now() + Duration::from_millis(c.interval_ms);
        let picture = self.screen.capture(c.width, c.quality);
        let fresh = picture.as_ref().ok().filter(|p| self.sent_hash != Some(p.hash));
        // Nothing new (unchanged, or no picture: e.g. a UAC prompt is in front): only a
        // short note every 2 s, so SMP (and BLOCK_GRACE) know this PC is still there.
        if fresh.is_none() && self.last_upload.elapsed() < UNCHANGED_NOTE_EVERY {
            return self.note_problem(picture.err().map(|e| format!("{e:#}")));
        }
        let upload = FrameUpload {
            kind: "remote",
            session: Some(&c.session),
            jpeg: fresh.map(|p| p.jpeg.as_slice()),
            width: picture.as_ref().map_or(0, |p| p.width),
            height: picture.as_ref().map_or(0, |p| p.height),
            screen_width: picture.as_ref().map_or(0, |p| p.screen_width),
            screen_height: picture.as_ref().map_or(0, |p| p.screen_height),
        };
        let sent = match self.client.put_frame(&self.client.unique(&c.frame_path), &upload) {
            // ...or the session just ended (the new config is on its way): SMP answered either way.
            Ok(()) => Ok(true),
            Err(e) if e.code() == Some("classroom.not_controlled") => Ok(false),
            Err(e) => Err(e.to_string()),
        };
        if sent.is_ok() {
            self.last_upload = Instant::now();
            self.last_contact = Instant::now();
        }
        if let (Ok(true), Some(p)) = (&sent, fresh) {
            self.sent_hash = Some(p.hash);
        }
        // A capture problem is worth reporting first; else the upload's.
        let problem = picture.err().map(|e| format!("{e:#}")).or(sent.err());
        self.note_problem(problem);
    }

    /// Log a problem once (not five times a second), and when it's gone.
    fn note_problem(&mut self, problem: Option<String>) {
        match problem {
            None if self.last_error.take().is_some() => log::info!("remote control: sending pictures again"),
            Some(e) if self.last_error.as_ref() != Some(&e) => {
                log::warn!("remote control: {e}");
                self.last_error = Some(e);
            }
            _ => {}
        }
    }
}

// ------------------------------------------------------------------------- input

#[derive(Debug, Deserialize)]
struct InputBatch {
    events: Vec<Value>,
}

/// Fetch the teacher's input and replay it, while a session is on.
fn input_loop(client: &Client, mut sink: Box<dyn InputSink>, inbox: &Receiver<Option<RemoteConfig>>) {
    let mut current: Option<RemoteConfig> = None;
    let mut pressed = Pressed::default();
    let mut last_error: Option<String> = None;
    loop {
        // The newest config wins; with no session, wait for one.
        let changed = match &current {
            None => match inbox.recv() {
                Ok(config) => Some(config),
                Err(_) => return,
            },
            Some(_) => inbox.try_iter().last(),
        };
        if let Some(config) = changed {
            if config.as_ref().map(|c| &c.session) != current.as_ref().map(|c| &c.session) {
                let_go(&mut *sink, &mut pressed); // a new or no session: nothing stays held
            }
            current = config;
        }
        let Some(c) = &current else { continue };
        let path = client.unique(&format!("{}?session={}", c.input_path, c.session));
        match client.get_json::<InputBatch>(&path, INPUT_TIMEOUT) {
            Ok(batch) => {
                let events = input::from_server(&batch.events);
                if !events.is_empty() {
                    pressed.track(&events);
                    sink.inject(&events);
                }
                last_error = None;
            }
            Err(e) if e.code() == Some("classroom.not_controlled") => {
                // Ended (the teacher closed the view): let go, wait for the next config.
                let_go(&mut *sink, &mut pressed);
                current = None;
            }
            Err(e) => {
                if matches!(e, ApiError::Transport(_)) {
                    client.reset_connections(); // pooled connections may be dead (like the heartbeat does)
                }
                let e = e.to_string();
                if last_error.as_ref() != Some(&e) {
                    log::warn!("remote control input: {e}");
                    last_error = Some(e);
                }
                std::thread::sleep(Duration::from_secs(1));
            }
        }
    }
}

fn let_go(sink: &mut dyn InputSink, pressed: &mut Pressed) {
    let released = pressed.release_all();
    if !released.is_empty() {
        sink.inject(&released);
    }
}

// ------------------------------------------------------------------------- without Windows

/// Test PCs (SMP_AGENT_FAKE_SCREEN): the fake screen with a black square where the
/// teacher's pointer is, so remote control can be tried end to end on Linux.
#[cfg_attr(windows, allow(dead_code))]
pub struct FakeRemote {
    pub screen: Option<crate::capabilities::screen::source::FakeScreen>,
    /// Where the teacher's pointer is (shared with LogInput), 0..=65535 each.
    pub pointer: Arc<std::sync::Mutex<(u16, u16)>>,
}

impl RemoteScreen for FakeRemote {
    fn capture(&mut self, max_width: u32, quality: u8) -> Result<Picture> {
        let pointer = *self.pointer.lock().unwrap();
        match &mut self.screen {
            Some(screen) => screen.capture_with_pointer(max_width, quality, Some(pointer)),
            None => anyhow::bail!("this platform has no screen capture; set SMP_AGENT_FAKE_SCREEN=1920x1080 for a test picture"),
        }
    }

    fn set_control(&mut self, controlled: bool, block_local: bool) {
        let state = match (controlled, block_local) {
            (true, true) => "REMOTE CONTROL, student's input blocked",
            (true, false) => "REMOTE CONTROL",
            (false, _) => "remote control ended",
        };
        log::info!("indicator: {state}");
    }
}

/// Logs the input (debug level) and moves the fake pointer.
#[cfg_attr(windows, allow(dead_code))]
pub struct LogInput {
    pub pointer: Arc<std::sync::Mutex<(u16, u16)>>,
}

impl InputSink for LogInput {
    fn inject(&mut self, events: &[InputEvent]) {
        for e in events {
            log::debug!("input: {e:?}");
            if let InputEvent::Move { x, y } = e {
                *self.pointer.lock().unwrap() = (*x, *y);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn section(remote: Value) -> Value {
        json!({"active": false, "remote": remote})
    }

    #[test]
    fn config_from_the_server() {
        assert_eq!(RemoteConfig::from_section(None), None);
        assert_eq!(RemoteConfig::from_section(Some(&json!({"active": false}))), None);
        let good = json!({"session": "9f3cab", "interval_ms": 10, "width": 99999, "quality": 1,
                          "frame_path": "/api/v1/classroom/agent/remote/frame",
                          "input_path": "/api/v1/classroom/agent/remote/input"});
        let c = RemoteConfig::from_section(Some(&section(good.clone()))).unwrap();
        assert_eq!((c.interval_ms, c.width, c.quality), (50, 3840, 20), "clamped");
        assert!(!c.block_local, "shared input unless the teacher blocks it");
        let mut blocked = good.clone();
        blocked["block_local"] = json!(true);
        assert!(RemoteConfig::from_section(Some(&section(blocked))).unwrap().block_local);

        let mut elsewhere = good.clone();
        elsewhere["input_path"] = json!("https://evil.example/input");
        assert_eq!(RemoteConfig::from_section(Some(&section(elsewhere))), None);
        let mut odd_session = good;
        odd_session["session"] = json!("x&n=1");
        assert_eq!(RemoteConfig::from_section(Some(&section(odd_session))), None);
    }

    /// Records what the platform was asked to do.
    #[derive(Default, Clone)]
    struct Log(Arc<std::sync::Mutex<Vec<String>>>);
    impl InputSink for Log {
        fn inject(&mut self, events: &[InputEvent]) {
            self.0.lock().unwrap().push(format!("{events:?}"));
        }
    }

    /// Records what the platform was told.
    struct Indicator(Arc<std::sync::Mutex<Vec<(bool, bool)>>>);
    impl RemoteScreen for Indicator {
        fn capture(&mut self, _: u32, _: u8) -> Result<Picture> {
            anyhow::bail!("not used here")
        }
        fn set_control(&mut self, controlled: bool, block_local: bool) {
            self.0.lock().unwrap().push((controlled, block_local));
        }
    }

    #[test]
    fn the_students_input_comes_back_without_contact() {
        let told = Arc::new(std::sync::Mutex::new(vec![]));
        let client = Arc::new(Client::new("https://smp.example", crate::core::identity::Identity::generate(), None));
        let mut w = FrameWorker::new(client, Box::new(Indicator(told.clone())));
        let config = |block_local| RemoteConfig {
            session: "s1".into(), interval_ms: 200, width: 1600, quality: 60, block_local,
            frame_path: "/api/f".into(), input_path: "/api/i".into(),
        };
        w.reconfigure(Some(config(false)));
        w.apply();
        w.reconfigure(Some(config(true))); // the teacher blocks the student's input
        w.apply();
        w.last_contact = Instant::now() - BLOCK_GRACE - Duration::from_secs(1); // SMP gone for a while
        w.apply();
        w.last_contact = Instant::now(); // back
        w.apply();
        w.reconfigure(None); // the session ends
        w.apply();
        assert_eq!(*told.lock().unwrap(), [(true, false), (true, true), (true, false), (true, true), (false, false)]);
    }

    #[test]
    fn letting_go_after_a_session() {
        let log = Log::default();
        let mut sink: Box<dyn InputSink> = Box::new(log.clone());
        let mut pressed = Pressed::default();
        let_go(&mut *sink, &mut pressed);
        assert!(log.0.lock().unwrap().is_empty(), "nothing held, nothing sent");
        pressed.track(&[InputEvent::Key { scan: 0x1D, extended: false, down: true }]);
        let_go(&mut *sink, &mut pressed);
        assert_eq!(log.0.lock().unwrap().as_slice(), ["[Key { scan: 29, extended: false, down: false }]"]);
    }
}
