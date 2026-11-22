// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Raw screen pixels -> a small JPEG. Pure Rust, the same on every platform.
//!
//! Pixels are BGRA (blue, green, red, alpha: 4 bytes each, rows top to bottom),
//! which is what Windows' GDI hands us. Alpha is ignored.

use std::hash::{DefaultHasher, Hasher};

use anyhow::{Context, Result};
use jpeg_encoder::{ColorType, Encoder};

/// A finished picture, ready to upload.
#[derive(Debug)]
pub struct Picture {
    pub jpeg: Vec<u8>,
    pub width: u32,
    pub height: u32,
    /// The real screen size (the UI uses it for the aspect ratio: 16:9, 4:3, 1:1, ...).
    pub screen_width: u32,
    pub screen_height: u32,
    /// Changes when the picture changes (to skip sending the same screen again).
    pub hash: u64,
}

/// Shrink to at most `max_width` (keeping the aspect ratio), then JPEG-encode.
pub fn encode(bgra: &[u8], width: u32, height: u32, max_width: u32, quality: u8) -> Result<Picture> {
    anyhow::ensure!(width > 0 && height > 0, "empty screen");
    anyhow::ensure!(bgra.len() == (width * height * 4) as usize, "pixel buffer doesn't match the size");
    let (small, w, h) = shrink(bgra, width, height, max_width);

    let mut hasher = DefaultHasher::new();
    hasher.write(&small);
    let hash = hasher.finish();

    let mut jpeg = Vec::with_capacity(small.len() / 10);
    Encoder::new(&mut jpeg, quality.clamp(1, 100))
        .encode(&small, w as u16, h as u16, ColorType::Bgra)
        .context("JPEG encoding")?;
    Ok(Picture { jpeg, width: w, height: h, screen_width: width, screen_height: height, hash })
}

/// Shrink by averaging every source pixel that falls into a target pixel ("area
/// averaging"): text stays readable, unlike simply skipping pixels. Never enlarges.
pub fn shrink(bgra: &[u8], width: u32, height: u32, max_width: u32) -> (Vec<u8>, u32, u32) {
    let out_w = max_width.clamp(1, width);
    if out_w == width {
        return (bgra.to_vec(), width, height);
    }
    // same aspect ratio: 1920x1080 -> 480x270, 1024x768 -> 480x360, 1080x1080 -> 480x480
    let out_h = ((height as u64 * out_w as u64 + width as u64 / 2) / width as u64).max(1) as u32;

    // Which source columns/rows make up each target column/row: [start, end)
    let span = |i: u32, out: u32, src: u32| {
        let start = (i as u64 * src as u64 / out as u64) as usize;
        let end = (((i + 1) as u64 * src as u64 / out as u64) as usize).max(start + 1);
        (start, end)
    };
    let cols: Vec<(usize, usize)> = (0..out_w).map(|x| span(x, out_w, width)).collect();

    let stride = width as usize * 4;
    let mut out = Vec::with_capacity(out_w as usize * out_h as usize * 4);
    for y in 0..out_h {
        let (y0, y1) = span(y, out_h, height);
        for &(x0, x1) in &cols {
            let mut sum = [0u32; 3];
            for row in y0..y1 {
                let line = &bgra[row * stride..];
                let (pixels, _) = line[x0 * 4..x1 * 4].as_chunks::<4>(); // [b, g, r, a] each
                for px in pixels {
                    sum[0] += px[0] as u32;
                    sum[1] += px[1] as u32;
                    sum[2] += px[2] as u32;
                }
            }
            let n = ((y1 - y0) * (x1 - x0)) as u32;
            out.extend_from_slice(&[(sum[0] / n) as u8, (sum[1] / n) as u8, (sum[2] / n) as u8, 255]);
        }
    }
    (out, out_w, out_h)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn screen(w: u32, h: u32) -> Vec<u8> {
        (0..w * h).flat_map(|i| [(i % 251) as u8, (i % 13) as u8, 200, 0]).collect()
    }

    #[test]
    fn keeps_the_aspect_ratio() {
        for (w, h, expect) in [(1920, 1080, (480, 270)), (1024, 768, (480, 360)), (1080, 1080, (480, 480)), (2560, 1080, (480, 203))] {
            let (px, ow, oh) = shrink(&screen(w, h), w, h, 480);
            assert_eq!((ow, oh), expect, "{w}x{h}");
            assert_eq!(px.len(), (ow * oh * 4) as usize);
        }
    }

    #[test]
    fn never_enlarges() {
        let (_, w, h) = shrink(&screen(320, 240), 320, 240, 480);
        assert_eq!((w, h), (320, 240));
    }

    #[test]
    fn averages_the_pixels_it_replaces() {
        // 2x2 -> 1x1: one black, three white pixels
        let px = [0, 0, 0, 0, 255, 255, 255, 0, 255, 255, 255, 0, 255, 255, 255, 0];
        let (out, w, h) = shrink(&px, 2, 2, 1);
        assert_eq!((w, h), (1, 1));
        assert_eq!(&out[..3], &[191, 191, 191]);
    }

    #[test]
    fn makes_a_jpeg_and_notices_changes() {
        let mut px = screen(640, 360);
        let a = encode(&px, 640, 360, 320, 60).unwrap();
        assert_eq!(&a.jpeg[..2], &[0xFF, 0xD8]);
        assert_eq!((a.width, a.height, a.screen_width, a.screen_height), (320, 180, 640, 360));
        assert_eq!(encode(&px, 640, 360, 320, 60).unwrap().hash, a.hash);
        px[1000] ^= 0xFF;
        px[1004] ^= 0xFF;
        assert_ne!(encode(&px, 640, 360, 320, 60).unwrap().hash, a.hash);
    }
}
