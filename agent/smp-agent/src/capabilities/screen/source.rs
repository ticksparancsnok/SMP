// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Where pictures come from. The capability doesn't care how a platform gets the
//! screen; it asks a `ScreenSource`:
//!
//!   Windows    a helper process in the user's session grabs the screen and shows
//!              the tray icon (platform/windows/session.rs); the service talks to it
//!   Linux      FakeScreen (a generated test picture) for development and demos,
//!              or NoScreen
//!
//! The source also owns the "you are being watched" indicator, because on Windows
//! the same helper shows both (and it must be visible whenever the agent runs).

use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{bail, Result};

use super::encode::{encode, Picture};
use crate::capabilities::lock::picture::locked_screen;
use crate::capabilities::lock::LockScreen;

pub trait ScreenSource: Send {
    /// The primary screen, shrunk to at most `max_width` pixels wide, as a JPEG.
    fn capture(&mut self, max_width: u32, quality: u8) -> Result<Picture>;

    /// Show whether a teacher is watching (Windows: coloured vs. grey tray icon).
    fn set_indicator(&mut self, monitoring: bool);
}

/// No screen to capture (a server, a build machine).
#[cfg_attr(windows, allow(dead_code))] // Windows uses the session helper
pub struct NoScreen;

impl ScreenSource for NoScreen {
    fn capture(&mut self, _max_width: u32, _quality: u8) -> Result<Picture> {
        bail!("this platform has no screen capture; set SMP_AGENT_FAKE_SCREEN=1920x1080 for a test picture")
    }

    fn set_indicator(&mut self, monitoring: bool) {
        log::info!("indicator: {}", if monitoring { "MONITORING ACTIVE" } else { "agent running" });
    }
}

/// A generated test picture of any size: colour bars, plus a block that moves every
/// 10 seconds (so you can see both "new picture" and "unchanged" uploads). While the
/// screen lock is on (`locked`, shared with the platform's LockTarget) it shows the
/// lock picture instead: background and logo (no text; there's no font here).
#[cfg_attr(windows, allow(dead_code))]
pub struct FakeScreen {
    pub width: u32,
    pub height: u32,
    pub locked: Arc<Mutex<Option<LockScreen>>>,
}

#[cfg_attr(windows, allow(dead_code))]
impl FakeScreen {
    /// From "1920x1080" (as in SMP_AGENT_FAKE_SCREEN).
    pub fn parse(size: &str) -> Option<Self> {
        let (w, h) = size.trim().split_once(['x', 'X'])?;
        let (width, height) = (w.parse().ok()?, h.parse().ok()?);
        (16..=7680).contains(&width).then_some(())?;
        (16..=4320).contains(&height).then_some(())?;
        Some(Self { width, height, locked: Arc::default() })
    }

    fn pixels(&self, step: u64) -> Vec<u8> {
        const BARS: [[u8; 3]; 7] = [[255, 255, 255], [0, 255, 255], [255, 255, 0], [0, 255, 0], [255, 0, 255], [0, 0, 255], [255, 0, 0]];
        let (w, h) = (self.width as usize, self.height as usize);
        let block = (w / 8).max(1);
        let bx = (step as usize * block) % (w - block + 1).max(1);
        let by = h / 2 - h.min(block) / 2;
        let mut px = Vec::with_capacity(w * h * 4);
        for y in 0..h {
            for x in 0..w {
                let [b, g, r] = if x >= bx && x < bx + block && y >= by && y < by + block {
                    [30, 30, 30]
                } else {
                    BARS[x * BARS.len() / w]
                };
                px.extend_from_slice(&[b, g, r, 255]);
            }
        }
        px
    }
}

impl ScreenSource for FakeScreen {
    fn capture(&mut self, max_width: u32, quality: u8) -> Result<Picture> {
        if let Some(lock) = self.locked.lock().unwrap().as_ref() {
            let pixels = locked_screen(self.width, self.height, lock.logo_png());
            return encode(&pixels, self.width, self.height, max_width, quality);
        }
        let step = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() / 10).unwrap_or(0);
        encode(&self.pixels(step), self.width, self.height, max_width, quality)
    }

    fn set_indicator(&mut self, monitoring: bool) {
        NoScreen.set_indicator(monitoring)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fake_screen_sizes() {
        assert!(FakeScreen::parse("1920x1080").is_some());
        assert!(FakeScreen::parse("1024X768").is_some());
        assert!(FakeScreen::parse("huge").is_none());
        assert!(FakeScreen::parse("0x0").is_none());
        let pic = FakeScreen::parse("1080x1080").unwrap().capture(480, 60).unwrap();
        assert_eq!((pic.width, pic.height, pic.screen_width), (480, 480, 1080));
    }

    #[test]
    fn a_locked_fake_screen_shows_the_lock() {
        let mut screen = FakeScreen::parse("640x360").unwrap();
        let normal = screen.capture(320, 60).unwrap();
        *screen.locked.lock().unwrap() = Some(LockScreen { message: "x".into(), logo: None });
        let locked = screen.capture(320, 60).unwrap();
        assert_ne!(normal.hash, locked.hash);
        assert_eq!((locked.width, locked.height), (320, 180));
    }
}
