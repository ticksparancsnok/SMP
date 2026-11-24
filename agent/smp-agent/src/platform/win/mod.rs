// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Windows 10/11.
//!
//! How the pieces fit:
//!
//!   service.rs   `smp-agent install` registers the "SMPAgent" service (runs as SYSTEM,
//!                starts with Windows). The service runs the normal agent loop.
//!   session.rs   Services run in session 0, which has no screen. So the service
//!                starts a helper (this same exe: `smp-agent session-helper`) in the
//!                session of whoever sits at the computer, and restarts it when the
//!                user logs off/on. They talk over a private named pipe.
//!   helper.rs    The helper: grabs the screen when asked (capture.rs) and always
//!                shows the SMP tray icon (tray.rs): grey = agent running,
//!                coloured = a teacher is watching. The icon is the GDPR notice.
//!                It also shows the lock screen when a teacher locks the computer
//!                (lock_window.rs, with keyboard and mouse blocked by input_block.rs).
//!   tray_visibility.rs  keeps that icon on the taskbar (not in the "^" overflow)
//!   wts.rs       Windows sessions: which one is active, who is logged in, starting
//!                a process in it.
//!   security.rs  DPAPI for the key file, and file permissions (SYSTEM + admins only).
//!   software.rs  installing software for capabilities/software: msiexec, detection
//!                rules (Windows Installer, registry), restarts (shutdown.exe).

mod capture;
pub mod helper;
mod input_block;
mod input_inject;
mod lock_window;
mod security;
mod session;
mod software;
pub mod service;
mod tray;
mod tray_visibility;
pub mod update;
mod wts;

use std::path::{Path, PathBuf};

use windows::core::HSTRING;
use windows::Win32::System::Registry::{RegGetValueW, HKEY_LOCAL_MACHINE, RRF_RT_REG_DWORD, RRF_RT_REG_SZ};

use windows::Win32::Globalization::GetSystemDefaultUILanguage;

use crate::capabilities::lock::LockTarget;
use crate::capabilities::screen::source::ScreenSource;

pub use security::{protect, restrict_file, unprotect};

/// End this process so Windows' service manager starts the agent again (its recovery
/// actions restart it after 5 s): the watchdog's restart. Never returns. (Run by hand,
/// `smp-agent run`, it just ends.)
pub fn restart_self() -> ! {
    log::logger().flush();
    std::process::exit(3)
}

/// C:\ProgramData\SMP (identity and logs). Only SYSTEM and administrators can read it.
pub fn state_dir() -> PathBuf {
    chosen_state_dir().0.clone()
}

/// What choosing the folder had to say (folders someone else made, skipped), for the
/// log: logging can only start once the folder (where the log files go) is known.
pub fn startup_notes() -> Vec<String> {
    chosen_state_dir().1.clone()
}

/// C:\ProgramData\SMP, or SMP-1, SMP-2, ... if someone else made that one before the
/// agent was installed (platform/state_dir.rs). Decided once per process; the same rule
/// finds the same folder at every start, also after an update.
fn chosen_state_dir() -> &'static (PathBuf, Vec<String>) {
    static CHOSEN: std::sync::OnceLock<(PathBuf, Vec<String>)> = std::sync::OnceLock::new();
    CHOSEN.get_or_init(|| {
        let base = std::env::var_os("ProgramData").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"));
        let (dir, mut notes) = crate::platform::state_dir::choose(&base, security::inspect_dir, security::create_private_dir);
        if let Some(dir) = dir {
            let _ = security::restrict_dir(&dir); // our permissions, also on folders made by older versions
            return (dir, notes);
        }
        // Someone took every name (a student scripting it, to keep the agent from
        // starting): a folder under Program Files, where only administrators create things.
        let program_files = std::env::var_os("ProgramFiles").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\Program Files"));
        let fallback = program_files.join("SMP").join("data");
        if security::inspect_dir(&fallback) == crate::platform::state_dir::Found::Missing {
            let _ = std::fs::create_dir_all(program_files.join("SMP"));
            let _ = security::create_private_dir(&fallback);
        }
        let _ = security::restrict_dir(&fallback);
        notes.push(format!("every SMP folder name in {} is taken; using {}", base.display(), fallback.display()));
        (fallback, notes)
    })
}

/// A log file under C:\ProgramData\SMP\logs (the service has no console). Keeps
/// one older file: <name>.old.
pub fn log_file(name: &str) -> Option<std::fs::File> {
    let dir = state_dir().join("logs");
    std::fs::create_dir_all(&dir).ok()?;
    let path = dir.join(name);
    if std::fs::metadata(&path).map(|m| m.len() > 5 * 1024 * 1024).unwrap_or(false) {
        let _ = std::fs::rename(&path, dir.join(format!("{name}.old")));
    }
    std::fs::OpenOptions::new().create(true).append(true).open(path).ok()
}

/// The Windows build number, e.g. 19045 (Windows 10 22H2) or 26100 (Windows 11 24H2).
fn windows_build() -> u32 {
    reg_string("CurrentBuildNumber").and_then(|b| b.parse().ok()).unwrap_or(0)
}

/// ("Windows 11 Pro", "23H2 (build 22631.4317)") from the registry.
pub fn os_info() -> (String, String) {
    let build = windows_build();
    // ProductName still says "Windows 10" on Windows 11; the build number tells them apart.
    let name = if build >= 22000 { "Windows 11" } else { "Windows 10" };
    let edition = match reg_string("EditionID").as_deref() {
        Some("Professional") => "Pro".to_string(),
        Some("Core") => "Home".to_string(),
        Some(other) => other.to_string(),
        None => String::new(),
    };
    let display = reg_string("DisplayVersion").or_else(|| reg_string("ReleaseId")).unwrap_or_default();
    let ubr = reg_dword("UBR").map(|u| format!(".{u}")).unwrap_or_default();
    (format!("{name} {edition}").trim().to_string(), format!("{display} (build {build}{ubr})").trim().to_string())
}

/// The user at the computer right now (None: nobody, or the login screen).
pub fn console_user() -> Option<String> {
    wts::interactive_session().and_then(wts::session_user)
}

/// Screen pictures come from the session helper (started here, once). Starting it
/// also puts the tray icon on the screen, whether or not anyone ever watches.
pub fn screen_source() -> Box<dyn ScreenSource> {
    Box::new(session::start())
}

/// The screen lock is shown by the same session helper (the same one helper: the
/// manager thread is started only once).
pub fn lock_target() -> Box<dyn LockTarget> {
    Box::new(session::start())
}

/// Installing and removing programs (capabilities/software): msiexec, the registry, shutdown.exe.
pub fn software_installer() -> Box<dyn crate::capabilities::software::Installer> {
    Box::new(software::WindowsInstaller)
}

/// Remote control goes through the same session helper: pictures with the pointer,
/// the tray's "controlled" state, and the teacher's input (its own pipe).
pub fn remote_screen() -> Box<dyn crate::capabilities::remote::RemoteScreen> {
    Box::new(session::start())
}

pub fn input_sink() -> Box<dyn crate::capabilities::remote::InputSink> {
    Box::new(session::start())
}

/// Is Windows' display language Hungarian? (The tray tooltip is Hungarian or English;
/// the lock screen follows SMP's block page language instead, see capabilities/lock.)
pub fn system_language_is_hungarian() -> bool {
    // SAFETY: plain query.
    unsafe { GetSystemDefaultUILanguage() & 0x3FF == 0x0E } // LANG_HUNGARIAN
}

const CURRENT_VERSION: &str = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion";

fn reg_string(value: &str) -> Option<String> {
    let mut buf = [0u16; 256];
    let mut size = std::mem::size_of_val(&buf) as u32;
    // SAFETY: buf and size describe a valid buffer we own.
    unsafe {
        RegGetValueW(
            HKEY_LOCAL_MACHINE,
            &HSTRING::from(CURRENT_VERSION),
            &HSTRING::from(value),
            RRF_RT_REG_SZ,
            None,
            Some(buf.as_mut_ptr().cast()),
            Some(&mut size),
        )
        .ok()
        .ok()?;
    }
    let chars = (size as usize / 2).saturating_sub(1); // without the terminating NUL
    Some(String::from_utf16_lossy(&buf[..chars])).filter(|s| !s.is_empty())
}

fn reg_dword(value: &str) -> Option<u32> {
    let mut data = 0u32;
    let mut size = 4u32;
    // SAFETY: data is a valid u32 and size says so.
    unsafe {
        RegGetValueW(
            HKEY_LOCAL_MACHINE,
            &HSTRING::from(CURRENT_VERSION),
            &HSTRING::from(value),
            RRF_RT_REG_DWORD,
            None,
            Some((&mut data as *mut u32).cast()),
            Some(&mut size),
        )
        .ok()
        .ok()?;
    }
    Some(data)
}

/// "C:\path" as a NUL-terminated wide string for Windows APIs.
fn wide(path: &Path) -> HSTRING {
    HSTRING::from(path.as_os_str())
}
