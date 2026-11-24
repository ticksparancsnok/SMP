// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! The lock screen's pictures, in plain Rust (so they're tested on Linux too).
//!
//! The logo arrives as PNG, usually with transparent parts. Instead of asking Windows
//! to blend it (premultiplied alpha, AlphaBlend), it is laid onto the lock screen's
//! background colour here, once: the result is an opaque BGRA picture that Windows
//! draws as it is. That works because the background is one flat colour.
//!
//! Colours: a light background with dark text. Most school logos are made for white
//! paper (dark lettering, transparent background), and would vanish on a dark screen.

use anyhow::{ensure, Context, Result};

use crate::capabilities::screen::encode::shrink;

/// Background of the lock screen, RGB (a very light blue-grey).
pub const BACKGROUND: [u8; 3] = [0xF1, 0xF5, 0xF9];
/// The message's colour, RGB (almost black, slightly blue).
#[cfg_attr(not(windows), allow(dead_code))] // only the Windows lock window draws text
pub const TEXT: [u8; 3] = [0x0F, 0x17, 0x2A];

/// Shown when the school hasn't uploaded its own logo (made by agent/tools/make_tray_icons.py).
pub const SMP_LOGO: &[u8] = include_bytes!("../../../assets/lock-logo.png");

/// The largest logo accepted, per side (SMP itself accepts at most this size).
pub const MAX_SIDE: u32 = 2048;

/// Reads one pixel of a decoded PNG row as RGBA.
type PixelReader = fn(&[u8]) -> [u8; 4];

/// An opaque picture: BGRA, rows top to bottom, alpha always 255.
#[derive(Debug, Clone, PartialEq)]
pub struct Image {
    pub bgra: Vec<u8>,
    pub width: u32,
    pub height: u32,
}

/// Decode `png`, shrink it to fit `max_width` x `max_height` (keeping its shape; never
/// enlarged) and lay it onto BACKGROUND.
pub fn logo_on_background(png: &[u8], max_width: u32, max_height: u32) -> Result<Image> {
    let (rgba, width, height) = decode_png(png)?;
    let full = flatten(&rgba, BACKGROUND);
    // The width that fits both limits: a tall logo is limited by the height.
    let fit_width = (width as u64 * max_height.max(1) as u64 / height as u64).max(1) as u32;
    let (bgra, width, height) = shrink(&full, width, height, max_width.max(1).min(fit_width));
    Ok(Image { bgra, width, height })
}

/// A whole locked screen as one picture: the background with the logo in the middle
/// of its upper part. The Windows lock window draws the logo and the text itself; this
/// is for screens that are only pictures (the fake screen of test PCs).
#[cfg_attr(windows, allow(dead_code))]
pub fn locked_screen(width: u32, height: u32, png: &[u8]) -> Vec<u8> {
    let [r, g, b] = BACKGROUND;
    let mut px: Vec<u8> = std::iter::repeat_n([b, g, r, 255], width as usize * height as usize).flatten().collect();
    if let Ok(logo) = logo_on_background(png, width / 3, height / 3) {
        let left = ((width - logo.width) / 2) as usize;
        let top = (height / 3 - logo.height / 2) as usize;
        let (stride, row_bytes) = (width as usize * 4, logo.width as usize * 4);
        for (y, row) in logo.bgra.chunks_exact(row_bytes).enumerate() {
            let start = (top + y) * stride + left * 4;
            px[start..start + row_bytes].copy_from_slice(row);
        }
    }
    px
}

/// PNG -> RGBA (8 bits per channel), with its width and height. Any PNG colour type
/// is accepted (grey, palette, 16-bit, with or without transparency).
pub fn decode_png(png: &[u8]) -> Result<(Vec<u8>, u32, u32)> {
    // Limits: a hostile file can't make us allocate more than this while decoding.
    let limits = png::Limits { bytes: 64 * 1024 * 1024 };
    let mut decoder = png::Decoder::new_with_limits(std::io::Cursor::new(png), limits);
    // EXPAND: palette and low bit depths -> 8-bit channels; STRIP_16: 16 -> 8 bits.
    decoder.set_transformations(png::Transformations::normalize_to_color8());
    let mut reader = decoder.read_info().context("the logo isn't a PNG image")?;
    let (width, height) = (reader.info().width, reader.info().height);
    ensure!(
        (1..=MAX_SIDE).contains(&width) && (1..=MAX_SIDE).contains(&height),
        "the logo is {width}x{height}; at most {MAX_SIDE}x{MAX_SIDE} is accepted"
    );
    let mut buf = vec![0; reader.output_buffer_size().context("the logo is too large")?];
    let frame = reader.next_frame(&mut buf).context("the logo PNG is damaged")?;

    // Every colour type the transformations above can leave, as bytes per pixel and
    // how to read one pixel into RGBA.
    let (channels, to_rgba): (usize, PixelReader) = match frame.color_type {
        png::ColorType::Rgba => (4, |p| [p[0], p[1], p[2], p[3]]),
        png::ColorType::Rgb => (3, |p| [p[0], p[1], p[2], 255]),
        png::ColorType::GrayscaleAlpha => (2, |p| [p[0], p[0], p[0], p[1]]),
        png::ColorType::Grayscale => (1, |p| [p[0], p[0], p[0], 255]),
        other => anyhow::bail!("unexpected PNG colour type {other:?}"),
    };
    let mut rgba = Vec::with_capacity(width as usize * height as usize * 4);
    // Rows may be padded: step by line_size, use only `width` pixels of each.
    for row in buf[..frame.buffer_size()].chunks_exact(frame.line_size).take(height as usize) {
        for pixel in row[..width as usize * channels].chunks_exact(channels) {
            rgba.extend_from_slice(&to_rgba(pixel));
        }
    }
    Ok((rgba, width, height))
}

/// RGBA with transparency -> opaque BGRA, as if drawn on a `background` (RGB) surface:
/// each colour = picture * alpha + background * (1 - alpha).
fn flatten(rgba: &[u8], background: [u8; 3]) -> Vec<u8> {
    let blend = |c: u8, bg: u8, a: u8| ((c as u32 * a as u32 + bg as u32 * (255 - a as u32) + 127) / 255) as u8;
    let [br, bgg, bb] = background;
    let (pixels, _) = rgba.as_chunks::<4>();
    pixels
        .iter()
        .flat_map(|p| [blend(p[2], bb, p[3]), blend(p[1], bgg, p[3]), blend(p[0], br, p[3]), 255])
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A PNG of `w` x `h` RGBA pixels, all `pixel`.
    fn png_of(w: u32, h: u32, pixel: [u8; 4]) -> Vec<u8> {
        let mut out = Vec::new();
        let mut encoder = png::Encoder::new(&mut out, w, h);
        encoder.set_color(png::ColorType::Rgba);
        encoder.set_depth(png::BitDepth::Eight);
        let mut writer = encoder.write_header().unwrap();
        writer.write_image_data(&pixel.repeat((w * h) as usize)).unwrap();
        writer.finish().unwrap();
        out
    }

    #[test]
    fn the_built_in_logo_decodes() {
        let (rgba, w, h) = decode_png(SMP_LOGO).unwrap();
        assert!(w > 100 && h > 100 && rgba.len() == (w * h * 4) as usize);
    }

    #[test]
    fn transparency_shows_the_background() {
        let clear = logo_on_background(&png_of(4, 4, [255, 0, 0, 0]), 100, 100).unwrap();
        let [r, g, b] = BACKGROUND;
        assert_eq!(&clear.bgra[..4], &[b, g, r, 255]);
        let solid = logo_on_background(&png_of(4, 4, [255, 0, 0, 255]), 100, 100).unwrap();
        assert_eq!(&solid.bgra[..4], &[0, 0, 255, 255]); // red, as BGRA
        let half = logo_on_background(&png_of(4, 4, [0, 0, 0, 128]), 100, 100).unwrap();
        // blue: 249 (background) * 127/255 (what shows through) = 124
        assert!((123..=125).contains(&half.bgra[0]), "half black over the light background: {}", half.bgra[0]);
    }

    #[test]
    fn fits_the_box_keeping_its_shape() {
        let wide = logo_on_background(&png_of(400, 100, [0, 0, 0, 255]), 200, 200).unwrap();
        assert_eq!((wide.width, wide.height), (200, 50));
        let tall = logo_on_background(&png_of(100, 400, [0, 0, 0, 255]), 200, 200).unwrap();
        assert_eq!((tall.width, tall.height), (50, 200));
        let small = logo_on_background(&png_of(40, 30, [0, 0, 0, 255]), 200, 200).unwrap();
        assert_eq!((small.width, small.height), (40, 30), "never enlarged");
    }

    #[test]
    fn rejects_what_isnt_a_usable_png() {
        assert!(decode_png(b"GIF89a....").is_err());
        assert!(decode_png(&png_of(MAX_SIDE + 1, 1, [0; 4])).is_err());
        let mut cut = png_of(64, 64, [1, 2, 3, 255]);
        cut.truncate(cut.len() / 2);
        assert!(decode_png(&cut).is_err());
    }

    #[test]
    fn a_locked_screen_picture_has_the_logo_in_it() {
        let px = locked_screen(640, 360, &png_of(30, 30, [0, 0, 0, 255]));
        assert_eq!(px.len(), 640 * 360 * 4);
        let at = |x: usize, y: usize| &px[(y * 640 + x) * 4..(y * 640 + x) * 4 + 3];
        assert_eq!(at(320, 120), &[0, 0, 0], "logo in the middle of the upper third");
        assert_eq!(at(5, 5), &[BACKGROUND[2], BACKGROUND[1], BACKGROUND[0]]);
    }
}
