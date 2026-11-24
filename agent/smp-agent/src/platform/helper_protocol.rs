// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! The little protocol between the Windows service and its session helper, over a
//! named pipe. Text lines, plus raw bytes after a PICTURE or LOCK line:
//!
//!   service -> helper            helper -> service
//!   CAPTURE <max_width> <q> <p>  PICTURE <w> <h> <screen_w> <screen_h> <hash> <len>\n<len bytes>
//!                                ERROR <message>
//!   INDICATOR <0|1>              OK
//!   CONTROL <0|1> <0|1>          OK
//!   LOCK <text_len> <png_len>\n<text bytes (UTF-8)><png bytes>
//!                                OK
//!   UNLOCK                       OK
//!
//! CAPTURE's <p>: 1 = draw the mouse pointer in (remote control). LOCK with png_len 0:
//! the SMP logo built into the agent. CONTROL: a teacher controls the computer (tray),
//! and whether they blocked the student's own keyboard and mouse.
//!
//! One request, one answer, in order. Kept free of Windows APIs so it's tested here.
//!
//! Remote control's input goes over a *second* pipe, one way (service -> helper), one
//! event per line, so it never waits behind a screen capture on the first one:
//!
//!   M <x> <y>                    pointer to x, y (0..65535 across the primary screen)
//!   B <l|m|r> <0|1>              mouse button up / down
//!   W <delta> <0|1>              wheel (120 = one notch); 1 = horizontal
//!   K <scan> <0|1> <0|1>         key: scan code, extended?, down?

use std::io::{self, BufRead, Read, Write};

use crate::capabilities::remote::input::{InputEvent, MouseButton};
use crate::capabilities::screen::encode::Picture;

const MAX_LINE: usize = 1024;
const MAX_JPEG: usize = 16 * 1024 * 1024;
const MAX_LOCK_TEXT: usize = 16 * 1024;
const MAX_LOCK_PNG: usize = 4 * 1024 * 1024;

#[derive(Debug, PartialEq)]
pub enum Request {
    Capture { max_width: u32, quality: u8, pointer: bool },
    Indicator { monitoring: bool },
    /// A teacher controls (true) or no longer controls the computer: the tray icon;
    /// `block_local`: only the teacher's input gets through.
    Control { controlled: bool, block_local: bool },
    /// Show the lock screen (or change what it shows). `png` empty: SMP's logo.
    Lock { text: String, png: Vec<u8> },
    Unlock,
}

pub fn write_request(w: &mut impl Write, request: &Request) -> io::Result<()> {
    match request {
        Request::Capture { max_width, quality, pointer } => writeln!(w, "CAPTURE {max_width} {quality} {}", u8::from(*pointer))?,
        Request::Indicator { monitoring } => writeln!(w, "INDICATOR {}", u8::from(*monitoring))?,
        Request::Control { controlled, block_local } => {
            writeln!(w, "CONTROL {} {}", u8::from(*controlled), u8::from(*block_local))?
        }
        Request::Lock { text, png } => {
            writeln!(w, "LOCK {} {}", text.len(), png.len())?;
            w.write_all(text.as_bytes())?;
            w.write_all(png)?;
        }
        Request::Unlock => writeln!(w, "UNLOCK")?,
    }
    w.flush()
}

/// The next request, or None when the service closed the pipe (it stopped).
pub fn read_request(r: &mut impl BufRead) -> io::Result<Option<Request>> {
    let Some(line) = read_line(r)? else { return Ok(None) };
    let words: Vec<&str> = line.split_whitespace().collect();
    let number = |i: usize| words.get(i).and_then(|w| w.parse::<u32>().ok());
    match (words.first().copied(), number(1), number(2)) {
        (Some("CAPTURE"), Some(max_width), Some(quality)) => {
            Ok(Some(Request::Capture { max_width, quality: quality.min(100) as u8, pointer: number(3) == Some(1) }))
        }
        (Some("INDICATOR"), Some(flag), _) => Ok(Some(Request::Indicator { monitoring: flag == 1 })),
        (Some("CONTROL"), Some(flag), block) => Ok(Some(Request::Control { controlled: flag == 1, block_local: block == Some(1) })),
        (Some("LOCK"), Some(text_len), Some(png_len)) => {
            let (text_len, png_len) = (text_len as usize, png_len as usize);
            if text_len > MAX_LOCK_TEXT || png_len > MAX_LOCK_PNG {
                return Err(bad("lock screen too large"));
            }
            let mut text = vec![0; text_len];
            r.read_exact(&mut text)?;
            let mut png = vec![0; png_len];
            r.read_exact(&mut png)?;
            let text = String::from_utf8(text).map_err(|_| bad("the lock text isn't UTF-8"))?;
            Ok(Some(Request::Lock { text, png }))
        }
        (Some("UNLOCK"), _, _) => Ok(Some(Request::Unlock)),
        _ => Err(bad(&format!("unknown request {line:?}"))),
    }
}

pub fn write_picture(w: &mut impl Write, result: Result<&Picture, String>) -> io::Result<()> {
    match result {
        Ok(p) => {
            let len = p.jpeg.len();
            writeln!(w, "PICTURE {} {} {} {} {} {len}", p.width, p.height, p.screen_width, p.screen_height, p.hash)?;
            w.write_all(&p.jpeg)?;
        }
        // one line: no line breaks inside the message
        Err(e) => writeln!(w, "ERROR {}", e.replace(['\r', '\n'], " "))?,
    }
    w.flush()
}

/// Ok(Err(message)) when the helper couldn't capture (e.g. the screen is locked);
/// Err(...) when the pipe itself failed.
pub fn read_picture(r: &mut impl BufRead) -> io::Result<Result<Picture, String>> {
    let line = read_line(r)?.ok_or_else(|| bad("the helper closed the pipe"))?;
    if let Some(message) = line.strip_prefix("ERROR ") {
        return Ok(Err(message.to_string()));
    }
    let n: Vec<u64> = match line.strip_prefix("PICTURE ") {
        Some(rest) => rest.split_whitespace().filter_map(|w| w.parse().ok()).collect(),
        None => return Err(bad(&format!("unexpected answer {line:?}"))),
    };
    let [w, h, sw, sh, hash, len] = n[..] else { return Err(bad("bad PICTURE line")) };
    if len as usize > MAX_JPEG {
        return Err(bad("picture too large"));
    }
    let mut jpeg = vec![0; len as usize];
    r.read_exact(&mut jpeg)?;
    Ok(Ok(Picture { jpeg, width: w as u32, height: h as u32, screen_width: sw as u32, screen_height: sh as u32, hash }))
}

pub fn write_ok(w: &mut impl Write) -> io::Result<()> {
    writeln!(w, "OK")?;
    w.flush()
}

pub fn read_ok(r: &mut impl BufRead) -> io::Result<()> {
    match read_line(r)?.as_deref() {
        Some("OK") => Ok(()),
        other => Err(bad(&format!("expected OK, got {other:?}"))),
    }
}

/// One input event as a line of the input pipe (see the top of this file).
pub fn write_input(w: &mut impl Write, event: &InputEvent) -> io::Result<()> {
    match *event {
        InputEvent::Move { x, y } => writeln!(w, "M {x} {y}"),
        InputEvent::Button { button, down } => {
            let b = match button {
                MouseButton::Left => 'l',
                MouseButton::Middle => 'm',
                MouseButton::Right => 'r',
            };
            writeln!(w, "B {b} {}", u8::from(down))
        }
        InputEvent::Wheel { delta, horizontal } => writeln!(w, "W {delta} {}", u8::from(horizontal)),
        InputEvent::Key { scan, extended, down } => writeln!(w, "K {scan} {} {}", u8::from(extended), u8::from(down)),
    }
}

/// The next input event, Ok(None) when the service closed the pipe. A line we don't
/// understand is an error: the two sides are the same exe, so it can't be "newer".
pub fn read_input(r: &mut impl BufRead) -> io::Result<Option<InputEvent>> {
    let Some(line) = read_line(r)? else { return Ok(None) };
    let w: Vec<&str> = line.split_whitespace().collect();
    let flag = |i: usize| match w.get(i) {
        Some(&"0") => Some(false),
        Some(&"1") => Some(true),
        _ => None,
    };
    let event = match (w.first().copied(), w.len()) {
        (Some("M"), 3) => w[1].parse().ok().zip(w[2].parse().ok()).map(|(x, y)| InputEvent::Move { x, y }),
        (Some("B"), 3) => {
            let button = match w[1] {
                "l" => Some(MouseButton::Left),
                "m" => Some(MouseButton::Middle),
                "r" => Some(MouseButton::Right),
                _ => None,
            };
            button.zip(flag(2)).map(|(button, down)| InputEvent::Button { button, down })
        }
        (Some("W"), 3) => w[1].parse().ok().zip(flag(2)).map(|(delta, horizontal)| InputEvent::Wheel { delta, horizontal }),
        (Some("K"), 4) => {
            let scan = w[1].parse().ok().filter(|s: &u16| *s <= 0xFF);
            scan.zip(flag(2)).zip(flag(3)).map(|((scan, extended), down)| InputEvent::Key { scan, extended, down })
        }
        _ => None,
    };
    event.map(Some).ok_or_else(|| bad(&format!("unknown input {line:?}")))
}

/// One line without its "\n"; None at the end of the stream.
fn read_line(r: &mut impl BufRead) -> io::Result<Option<String>> {
    let mut line = String::new();
    let n = r.take(MAX_LINE as u64).read_line(&mut line)?;
    if n == 0 {
        return Ok(None);
    }
    if !line.ends_with('\n') {
        return Err(bad("line too long or cut off"));
    }
    Ok(Some(line.trim_end().to_string()))
}

fn bad(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn requests_round_trip() {
        let mut buf = vec![];
        write_request(&mut buf, &Request::Capture { max_width: 480, quality: 60, pointer: false }).unwrap();
        write_request(&mut buf, &Request::Capture { max_width: 1600, quality: 60, pointer: true }).unwrap();
        write_request(&mut buf, &Request::Indicator { monitoring: true }).unwrap();
        write_request(&mut buf, &Request::Control { controlled: true, block_local: true }).unwrap();
        let mut r = Cursor::new(buf);
        assert_eq!(read_request(&mut r).unwrap(), Some(Request::Capture { max_width: 480, quality: 60, pointer: false }));
        assert_eq!(read_request(&mut r).unwrap(), Some(Request::Capture { max_width: 1600, quality: 60, pointer: true }));
        assert_eq!(read_request(&mut r).unwrap(), Some(Request::Indicator { monitoring: true }));
        assert_eq!(read_request(&mut r).unwrap(), Some(Request::Control { controlled: true, block_local: true }));
        assert_eq!(read_request(&mut r).unwrap(), None); // service gone
        assert!(read_request(&mut Cursor::new(b"FORMAT C:\n".to_vec())).is_err());
    }

    #[test]
    fn input_round_trips() {
        let events = [
            InputEvent::Move { x: 0, y: 65535 },
            InputEvent::Button { button: MouseButton::Middle, down: true },
            InputEvent::Wheel { delta: -240, horizontal: true },
            InputEvent::Key { scan: 0x5B, extended: true, down: false },
        ];
        let mut buf = vec![];
        for e in &events {
            write_input(&mut buf, e).unwrap();
        }
        let mut r = Cursor::new(buf);
        for e in events {
            assert_eq!(read_input(&mut r).unwrap(), Some(e));
        }
        assert_eq!(read_input(&mut r).unwrap(), None, "the service closed the pipe");
        for bad in ["M 1\n", "M 70000 1\n", "B x 1\n", "K 999 0 1\n", "K 30 2 1\n", "FORMAT C:\n"] {
            assert!(read_input(&mut Cursor::new(bad.as_bytes().to_vec())).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn lock_requests_round_trip() {
        let lock = Request::Lock { text: "Figyelj a tanárra!\nKöszönöm".into(), png: vec![0x89, b'P', b'N', b'G', b'\n', 0] };
        let mut buf = vec![];
        write_request(&mut buf, &lock).unwrap();
        write_request(&mut buf, &Request::Lock { text: String::new(), png: vec![] }).unwrap();
        write_request(&mut buf, &Request::Unlock).unwrap();
        write_request(&mut buf, &Request::Indicator { monitoring: false }).unwrap(); // still in step
        let mut r = Cursor::new(buf);
        assert_eq!(read_request(&mut r).unwrap(), Some(lock));
        assert_eq!(read_request(&mut r).unwrap(), Some(Request::Lock { text: String::new(), png: vec![] }));
        assert_eq!(read_request(&mut r).unwrap(), Some(Request::Unlock));
        assert_eq!(read_request(&mut r).unwrap(), Some(Request::Indicator { monitoring: false }));

        let huge = format!("LOCK 1 {}\n", MAX_LOCK_PNG + 1);
        assert!(read_request(&mut Cursor::new(huge.into_bytes())).is_err());
        assert!(read_request(&mut Cursor::new(b"LOCK 2 0\n\xff\xfe".to_vec())).is_err(), "not UTF-8");
        assert!(read_request(&mut Cursor::new(b"LOCK 10 0\nshort".to_vec())).is_err(), "cut off");
    }

    #[test]
    fn pictures_and_errors_round_trip() {
        let pic = Picture { jpeg: vec![0xFF, 0xD8, 1, 2, 3, b'\n'], width: 480, height: 270, screen_width: 1920, screen_height: 1080, hash: 42 };
        let mut buf = vec![];
        write_picture(&mut buf, Ok(&pic)).unwrap();
        write_picture(&mut buf, Err("screen locked\nreally".into())).unwrap();
        write_ok(&mut buf).unwrap();
        let mut r = Cursor::new(buf);
        let got = read_picture(&mut r).unwrap().unwrap();
        assert_eq!((got.jpeg, got.width, got.screen_height, got.hash), (pic.jpeg, 480, 1080, 42));
        assert_eq!(read_picture(&mut r).unwrap().unwrap_err(), "screen locked really");
        read_ok(&mut r).unwrap();
        assert!(read_picture(&mut r).is_err()); // pipe closed
    }
}
