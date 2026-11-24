// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Screen monitoring: send pictures of the screen while a teacher watches the room.
//!
//! The server decides everything, in the config section "classroom" (the server
//! module's name). Example while the room is watched:
//!
//!   {"active": true, "interval_s": 5, "width": 480, "quality": 60,
//!    "detail": false, "detail_width": 1600, "detail_interval_s": 2,
//!    "upload_path": "/api/v1/classroom/frames"}
//!
//! and {"active": false} otherwise: then nothing is captured or sent at all.
//! "detail" is true only for the one PC a teacher enlarged; it then also sends a
//! bigger picture, more often.
//!
//! Bandwidth (schools often have 100 Mbps): a 480 px thumbnail is ~15-40 KB, so a
//! watched room of 30 PCs at 5 s is about 1-2 Mbit/s. A screen that didn't change
//! (a test, a document) is confirmed with an empty request instead of the picture.
//!
//! Threads: `apply_config` (called by the main loop) only sends the new config over a
//! channel; a worker thread does the capturing and uploading, so a slow upload never
//! delays heartbeats.

pub mod encode;
pub mod source;

use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::Value;

use crate::core::api::{ApiError, Client, FrameUpload};
use crate::core::capability::Capability;
use source::ScreenSource;

/// What the server asks for. Missing fields get safe defaults; numbers are clamped
/// so a wrong value can't flood the network.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default)]
pub struct ScreenConfig {
    pub active: bool,
    pub interval_s: u64,
    pub width: u32,
    pub quality: u8,
    pub detail: bool,
    pub detail_width: u32,
    pub detail_interval_s: u64,
    pub upload_path: String,
}

impl Default for ScreenConfig {
    fn default() -> Self {
        Self {
            active: false,
            interval_s: 5,
            width: 480,
            quality: 60,
            detail: false,
            detail_width: 1600,
            detail_interval_s: 2,
            upload_path: "/api/v1/classroom/frames".into(),
        }
    }
}

impl ScreenConfig {
    pub fn from_section(section: Option<&Value>) -> Self {
        let mut c: Self = section.and_then(|v| serde_json::from_value(v.clone()).ok()).unwrap_or_default();
        c.interval_s = c.interval_s.clamp(1, 300);
        c.detail_interval_s = c.detail_interval_s.clamp(1, 60);
        c.width = c.width.clamp(160, 1280);
        c.detail_width = c.detail_width.clamp(640, 3840);
        c.quality = c.quality.clamp(20, 95);
        if !c.upload_path.starts_with("/api/") {
            c.upload_path = Self::default().upload_path; // only ever upload to our own API
        }
        c
    }
}

enum Message {
    Config(ScreenConfig),
    Stop,
}

pub struct Screen {
    to_worker: Sender<Message>,
    worker: Option<JoinHandle<()>>,
}

impl Screen {
    pub fn new(client: Arc<Client>, source: Box<dyn ScreenSource>) -> Self {
        let (to_worker, inbox) = mpsc::channel();
        let worker = std::thread::Builder::new()
            .name("screen".into())
            .spawn(move || Worker::new(client, source).run(inbox))
            .expect("starting the screen thread");
        Self { to_worker, worker: Some(worker) }
    }
}

impl Capability for Screen {
    fn name(&self) -> &'static str {
        "classroom"
    }

    fn apply_config(&mut self, section: Option<&Value>) {
        let _ = self.to_worker.send(Message::Config(ScreenConfig::from_section(section)));
    }

    fn stop(&mut self) {
        let _ = self.to_worker.send(Message::Stop);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

/// One kind of picture ("thumb" or "detail") with its own timer.
struct Stream {
    kind: &'static str,
    next: Instant,
    /// Hash of the last picture the server accepted (None: send a full picture).
    sent_hash: Option<u64>,
    last_error: Option<String>,
}

impl Stream {
    fn new(kind: &'static str) -> Self {
        Self { kind, next: Instant::now(), sent_hash: None, last_error: None }
    }

    fn reset(&mut self) {
        self.next = Instant::now();
        self.sent_hash = None;
    }
}

struct Worker {
    client: Arc<Client>,
    source: Box<dyn ScreenSource>,
    config: ScreenConfig,
    thumb: Stream,
    detail: Stream,
}

impl Worker {
    fn new(client: Arc<Client>, source: Box<dyn ScreenSource>) -> Self {
        let config = ScreenConfig::default();
        Self { client, source, config, thumb: Stream::new("thumb"), detail: Stream::new("detail") }
    }

    fn run(mut self, inbox: Receiver<Message>) {
        self.source.set_indicator(false);
        loop {
            // Sleep until the next picture is due, or until a new config arrives.
            let wait = self.next_due().map(|due| due.saturating_duration_since(Instant::now()));
            let message = match wait {
                Some(wait) => inbox.recv_timeout(wait),
                None => inbox.recv().map_err(|_| RecvTimeoutError::Disconnected),
            };
            match message {
                Ok(Message::Config(config)) => self.reconfigure(config),
                Ok(Message::Stop) | Err(RecvTimeoutError::Disconnected) => break,
                Err(RecvTimeoutError::Timeout) => self.send_due(),
            }
        }
        self.source.set_indicator(false);
    }

    fn next_due(&self) -> Option<Instant> {
        match (self.config.active, self.config.detail) {
            (false, _) => None,
            (true, false) => Some(self.thumb.next),
            (true, true) => Some(self.thumb.next.min(self.detail.next)),
        }
    }

    fn reconfigure(&mut self, config: ScreenConfig) {
        if config == self.config {
            return;
        }
        if config.active != self.config.active {
            log::info!("screen monitoring {}", if config.active { "started" } else { "stopped" });
            self.source.set_indicator(config.active);
            self.thumb.reset(); // a viewer just arrived: send right away
        }
        if config.detail && !self.config.detail {
            self.detail.reset(); // just enlarged: the big picture right away
        }
        self.config = config;
    }

    fn send_due(&mut self) {
        let now = Instant::now();
        let c = self.config.clone();
        if c.active && self.thumb.next <= now {
            Self::send(&self.client, self.source.as_mut(), &mut self.thumb, &c.upload_path, c.width, c.quality);
            self.thumb.next = now + Duration::from_secs(c.interval_s);
        }
        if c.active && c.detail && self.detail.next <= now {
            Self::send(&self.client, self.source.as_mut(), &mut self.detail, &c.upload_path, c.detail_width, c.quality);
            self.detail.next = now + Duration::from_secs(c.detail_interval_s);
        }
    }

    fn send(client: &Client, source: &mut dyn ScreenSource, stream: &mut Stream, path: &str, width: u32, quality: u8) {
        let result = source.capture(width, quality).map_err(|e| format!("{e:#}")).and_then(|picture| {
            let same = stream.sent_hash == Some(picture.hash);
            let mut upload = FrameUpload {
                kind: stream.kind,
                session: None,
                jpeg: if same { None } else { Some(&picture.jpeg) },
                width: picture.width,
                height: picture.height,
                screen_width: picture.screen_width,
                screen_height: picture.screen_height,
            };
            let mut result = client.put_frame(path, &upload);
            if same && matches!(&result, Err(e) if e.code() == Some("classroom.need_frame")) {
                upload.jpeg = Some(&picture.jpeg); // the server lost it (restart): send it again
                result = client.put_frame(path, &upload);
            }
            match result {
                Ok(()) => {
                    log::debug!("{}: {}", stream.kind, if same { "unchanged".into() } else { format!("{} bytes", picture.jpeg.len()) });
                    stream.sent_hash = Some(picture.hash);
                    Ok(())
                }
                // Nobody watches any more; the new config is on its way. Not an error.
                Err(e) if e.code() == Some("classroom.not_watched") => Ok(()),
                Err(ApiError::Unauthorized) => Err("the server doesn't accept this device".into()),
                Err(e) => Err(e.to_string()),
            }
        });
        // Log a problem once, not every few seconds (e.g. a locked screen can't be captured).
        match result {
            Ok(()) if stream.last_error.is_some() => {
                log::info!("{}: sending pictures again", stream.kind);
                stream.last_error = None;
            }
            Err(e) if stream.last_error.as_ref() != Some(&e) => {
                log::warn!("{}: {e}", stream.kind);
                stream.last_error = Some(e);
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn config_from_the_server() {
        assert!(!ScreenConfig::from_section(None).active);
        assert!(!ScreenConfig::from_section(Some(&json!({"active": false}))).active);
        let c = ScreenConfig::from_section(Some(&json!({"active": true, "interval_s": 0, "width": 99999, "quality": 5})));
        assert!(c.active);
        assert_eq!((c.interval_s, c.width, c.quality), (1, 1280, 20)); // clamped
        let evil = ScreenConfig::from_section(Some(&json!({"active": true, "upload_path": "https://elsewhere/"})));
        assert_eq!(evil.upload_path, "/api/v1/classroom/frames");
    }
}
