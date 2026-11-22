//! `smp-agent session-helper`: runs in the user's session (started by the service,
//! see session.rs). Shows the tray icon from the first moment, and captures the
//! screen when the service asks. It exits when the service closes the pipe, so the
//! icon disappears exactly when the agent stops.

use std::fs::File;
use std::io::BufReader;
use std::os::windows::io::AsRawHandle;

use anyhow::{bail, Context, Result};
use windows::Win32::Foundation::HANDLE;
use windows::Win32::System::Pipes::GetNamedPipeServerProcessId;
use windows::Win32::UI::HiDpi::{SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2};

use super::{capture, tray, tray_visibility};
use crate::capabilities::screen::encode::encode;
use crate::platform::helper_protocol::{self as protocol, Request};

pub fn run(pipe_name: &str, server_pid: u32) -> Result<()> {
    // Real pixels, not the scaled-down picture Windows gives "DPI-unaware" programs
    // (a 4K screen at 150 % would otherwise come out as 2560x1440, blurred).
    // SAFETY: no pointers involved; fails harmlessly if already set.
    let _ = unsafe { SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) };

    let tray = tray::Tray::start().context("tray icon")?;
    tray_visibility::keep_promoted(tray.notifier()); // Windows 11: never in the "^" overflow

    let pipe = std::fs::OpenOptions::new().read(true).write(true).open(pipe_name).context("opening the service's pipe")?;
    let mut server = 0u32;
    // SAFETY: a valid pipe handle; plain out-parameter.
    unsafe { GetNamedPipeServerProcessId(HANDLE(pipe.as_raw_handle()), &mut server) }.context("checking the pipe")?;
    if server != server_pid {
        bail!("the pipe doesn't belong to the SMP service");
    }
    log::info!("connected to the service (process {server_pid})");

    let mut reader = BufReader::new(pipe.try_clone()?);
    let mut writer: File = pipe;
    while let Some(request) = protocol::read_request(&mut reader)? {
        match request {
            Request::Capture { max_width, quality } => {
                let picture = capture::primary_screen()
                    .and_then(|screen| encode(&screen.bgra, screen.width, screen.height, max_width, quality))
                    .map_err(|e| format!("{e:#}"));
                protocol::write_picture(&mut writer, picture.as_ref().map_err(Clone::clone))?;
            }
            Request::Indicator { monitoring } => {
                tray.set_monitoring(monitoring);
                protocol::write_ok(&mut writer)?;
            }
        }
    }
    log::info!("the service closed the pipe; exiting");
    Ok(()) // dropping `tray` removes the icon
}
