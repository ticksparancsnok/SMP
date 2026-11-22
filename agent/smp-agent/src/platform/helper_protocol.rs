// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! The little protocol between the Windows service and its session helper, over a
//! named pipe. Text lines, plus the JPEG bytes after a PICTURE line:
//!
//!   service -> helper            helper -> service
//!   CAPTURE <max_width> <q>      PICTURE <w> <h> <screen_w> <screen_h> <hash> <len>\n<len bytes>
//!                                ERROR <message>
//!   INDICATOR <0|1>              OK
//!
//! One request, one answer, in order. Kept free of Windows APIs so it's tested here.

use std::io::{self, BufRead, Read, Write};

use crate::capabilities::screen::encode::Picture;

const MAX_LINE: usize = 1024;
const MAX_JPEG: usize = 16 * 1024 * 1024;

#[derive(Debug, PartialEq)]
pub enum Request {
    Capture { max_width: u32, quality: u8 },
    Indicator { monitoring: bool },
}

pub fn write_request(w: &mut impl Write, request: &Request) -> io::Result<()> {
    match request {
        Request::Capture { max_width, quality } => writeln!(w, "CAPTURE {max_width} {quality}")?,
        Request::Indicator { monitoring } => writeln!(w, "INDICATOR {}", u8::from(*monitoring))?,
    }
    w.flush()
}

/// The next request, or None when the service closed the pipe (it stopped).
pub fn read_request(r: &mut impl BufRead) -> io::Result<Option<Request>> {
    let Some(line) = read_line(r)? else { return Ok(None) };
    let words: Vec<&str> = line.split_whitespace().collect();
    let number = |i: usize| words.get(i).and_then(|w| w.parse::<u32>().ok());
    match (words.first().copied(), number(1), number(2)) {
        (Some("CAPTURE"), Some(max_width), Some(quality)) => Ok(Some(Request::Capture { max_width, quality: quality.min(100) as u8 })),
        (Some("INDICATOR"), Some(flag), _) => Ok(Some(Request::Indicator { monitoring: flag == 1 })),
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
        write_request(&mut buf, &Request::Capture { max_width: 480, quality: 60 }).unwrap();
        write_request(&mut buf, &Request::Indicator { monitoring: true }).unwrap();
        let mut r = Cursor::new(buf);
        assert_eq!(read_request(&mut r).unwrap(), Some(Request::Capture { max_width: 480, quality: 60 }));
        assert_eq!(read_request(&mut r).unwrap(), Some(Request::Indicator { monitoring: true }));
        assert_eq!(read_request(&mut r).unwrap(), None); // service gone
        assert!(read_request(&mut Cursor::new(b"FORMAT C:\n".to_vec())).is_err());
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
