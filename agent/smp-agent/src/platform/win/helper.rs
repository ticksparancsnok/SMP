// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! `smp-agent session-helper`: runs in the user's session (started by the service,
//! see session.rs). Shows the tray icon from the first moment, captures the screen
//! and shows the lock screen when the service asks, and replays a teacher's input
//! during remote control (from a second pipe, on a thread of its own). It exits when
//! the service closes the pipe, so the icon and any lock disappear exactly when the
//! agent stops.

use std::fs::File;
use std::io::BufReader;
use std::os::windows::io::AsRawHandle;

use anyhow::{bail, Context, Result};
use windows::Win32::Foundation::HANDLE;
use windows::Win32::System::Pipes::GetNamedPipeServerProcessId;
use windows::Win32::UI::HiDpi::{SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2};

use super::input_block::{self, InputBlock, Mode};
use super::lock_window::LockWindow;
use super::{capture, input_inject, tray, tray_visibility};
use crate::capabilities::lock::picture::SMP_LOGO;
use crate::capabilities::screen::encode::encode;
use crate::platform::helper_protocol::{self as protocol, Request};

pub fn run(pipe_name: &str, input_pipe_name: &str, server_pid: u32) -> Result<()> {
    // Real pixels, not the scaled-down picture Windows gives "DPI-unaware" programs
    // (a 4K screen at 150 % would otherwise come out as 2560x1440, blurred).
    // SAFETY: no pointers involved; fails harmlessly if already set.
    let _ = unsafe { SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) };

    let tray = tray::Tray::start().context("tray icon")?;
    tray_visibility::keep_promoted(tray.notifier()); // Windows 11: never in the "^" overflow

    // The main pipe first, then the input pipe: the service waits for them in this order.
    let pipe = open_pipe(pipe_name, server_pid)?;
    let input = open_pipe(input_pipe_name, server_pid)?;
    log::info!("connected to the service (process {server_pid})");
    std::thread::Builder::new().name("input".into()).spawn(move || replay_input(input))?;

    let mut reader = BufReader::new(pipe.try_clone()?);
    let mut writer: File = pipe;
    let mut lock: Option<LockWindow> = None; // dropping it unlocks
    let mut blocking = Blocking::default();
    while let Some(request) = protocol::read_request(&mut reader)? {
        match request {
            Request::Capture { max_width, quality, pointer } => {
                let picture = capture::primary_screen(pointer)
                    .and_then(|screen| encode(&screen.bgra, screen.width, screen.height, max_width, quality))
                    .map_err(|e| format!("{e:#}"));
                protocol::write_picture(&mut writer, picture.as_ref().map_err(Clone::clone))?;
            }
            Request::Indicator { monitoring } => {
                tray.set_monitoring(monitoring);
                protocol::write_ok(&mut writer)?;
            }
            Request::Control { controlled, block_local } => {
                tray.set_controlled(controlled);
                blocking.only_teacher = controlled && block_local;
                blocking.update();
                protocol::write_ok(&mut writer)?;
            }
            Request::Lock { text, png } => {
                let png = if png.is_empty() { SMP_LOGO.to_vec() } else { png };
                match &lock {
                    Some(window) => window.update(text, png),
                    None => match LockWindow::start(text, png) {
                        Ok(window) => lock = Some(window),
                        Err(e) => log::error!("couldn't lock the screen: {e:#}"),
                    },
                }
                blocking.locked = true; // keyboard and mouse even if the window failed
                blocking.update();
                protocol::write_ok(&mut writer)?;
            }
            Request::Unlock => {
                lock = None;
                blocking.locked = false;
                blocking.update();
                protocol::write_ok(&mut writer)?;
            }
        }
    }
    log::info!("the service closed the pipe; exiting");
    Ok(()) // dropping `lock` unlocks, dropping `tray` removes the icon
}

/// The one place that decides whether keyboard and mouse are blocked, and how:
///
///   locked                       everything (the screen lock)
///   remote control, blocked      only the teacher's input gets through
///   otherwise                    nothing blocked
///
/// The lock wins (a lock ends remote control on the server anyway). The hooks run
/// while `block` exists; switching between the two modes keeps them running.
#[derive(Default)]
struct Blocking {
    locked: bool,
    only_teacher: bool,
    block: Option<InputBlock>,
}

impl Blocking {
    fn update(&mut self) {
        let mode = match (self.locked, self.only_teacher) {
            (true, _) => Some(Mode::All),
            (false, true) => Some(Mode::OnlyTeacher),
            (false, false) => None,
        };
        match mode {
            Some(mode) => {
                input_block::set_mode(mode); // before the hooks start: no moment in the wrong mode
                if self.block.is_none() {
                    match InputBlock::start() {
                        Ok(block) => self.block = Some(block),
                        Err(e) => log::error!("keyboard and mouse stay usable: {e:#}"),
                    }
                }
                log::info!("input: {}", if mode == Mode::All { "all blocked (lock)" } else { "only the teacher's" });
            }
            None => self.block = None, // dropping it removes the hooks
        }
    }
}

/// Open one of the service's pipes, and make sure it really is the SMP service's (a
/// pipe name can't be reserved: someone else could have made one with the same name).
fn open_pipe(name: &str, server_pid: u32) -> Result<File> {
    let pipe = std::fs::OpenOptions::new().read(true).write(true).open(name).context("opening the service's pipe")?;
    let mut server = 0u32;
    // SAFETY: a valid pipe handle; plain out-parameter.
    unsafe { GetNamedPipeServerProcessId(HANDLE(pipe.as_raw_handle()), &mut server) }.context("checking the pipe")?;
    if server != server_pid {
        bail!("the pipe doesn't belong to the SMP service");
    }
    Ok(pipe)
}

/// Remote control: the teacher's input, one event per line, replayed as it comes.
/// Ends when the service closes the pipe (or sends something we can't read).
fn replay_input(pipe: File) {
    let mut reader = BufReader::new(pipe);
    loop {
        match protocol::read_input(&mut reader) {
            Ok(Some(event)) => input_inject::send(&[event]),
            Ok(None) => return,
            Err(e) => {
                log::warn!("input pipe: {e}");
                return;
            }
        }
    }
}
