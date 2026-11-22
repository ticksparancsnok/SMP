// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Grabbing the primary screen with GDI (works on every Windows 10/11, also in VMs
//! and over remote desktop). The primary screen is the one whose top-left corner is
//! (0, 0) in screen coordinates; other monitors are left out.
//!
//! The mouse pointer isn't included, and the lock screen / UAC prompts (the "secure
//! desktop") can't be captured: then this returns an error and nothing is sent.

use anyhow::{bail, Result};
use windows::Win32::Graphics::Gdi::{
    BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject, GetDC, GetDIBits, ReleaseDC, SelectObject,
    BITMAPINFO, BITMAPINFOHEADER, BI_RGB, DIB_RGB_COLORS, SRCCOPY,
};
use windows::Win32::UI::WindowsAndMessaging::{GetSystemMetrics, SM_CXSCREEN, SM_CYSCREEN};

pub struct Screen {
    /// BGRA, top row first
    pub bgra: Vec<u8>,
    pub width: u32,
    pub height: u32,
}

pub fn primary_screen() -> Result<Screen> {
    // SAFETY: every GDI object created here is released before returning.
    unsafe {
        let (w, h) = (GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN));
        if w <= 0 || h <= 0 {
            bail!("no screen");
        }
        let screen = GetDC(None);
        if screen.is_invalid() {
            bail!("no access to the screen");
        }
        let memory = CreateCompatibleDC(Some(screen));
        let bitmap = CreateCompatibleBitmap(screen, w, h);
        let previous = SelectObject(memory, bitmap.into());

        let copied = BitBlt(memory, 0, 0, w, h, Some(screen), 0, 0, SRCCOPY);

        let mut info = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w,
                biHeight: -h, // negative: top row first
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut bgra = vec![0u8; w as usize * h as usize * 4];
        SelectObject(memory, previous); // a bitmap must not be selected while GetDIBits reads it
        let rows = if copied.is_ok() {
            GetDIBits(memory, bitmap, 0, h as u32, Some(bgra.as_mut_ptr().cast()), &mut info, DIB_RGB_COLORS)
        } else {
            0
        };

        let _ = DeleteObject(bitmap.into());
        let _ = DeleteDC(memory);
        ReleaseDC(None, screen);

        if let Err(e) = copied {
            bail!("screen copy failed (locked screen or UAC prompt?): {e}");
        }
        if rows != h {
            bail!("reading the screen failed");
        }
        Ok(Screen { bgra, width: w as u32, height: h as u32 })
    }
}
