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
//!   tray_visibility.rs  keeps that icon on the taskbar (not in the "^" overflow)
//!   wts.rs       Windows sessions: which one is active, who is logged in, starting
//!                a process in it.
//!   security.rs  DPAPI for the key file, and file permissions (SYSTEM + admins only).

mod capture;
pub mod helper;
mod security;
mod session;
pub mod service;
mod tray;
mod tray_visibility;
mod wts;

use std::path::{Path, PathBuf};

use windows::core::HSTRING;
use windows::Win32::System::Registry::{RegGetValueW, HKEY_LOCAL_MACHINE, RRF_RT_REG_DWORD, RRF_RT_REG_SZ};

use crate::capabilities::screen::source::ScreenSource;

pub use security::{protect, restrict_file, unprotect};

/// C:\ProgramData\SMP (identity and logs). Only SYSTEM and administrators can read it.
pub fn state_dir() -> PathBuf {
    let base = std::env::var_os("ProgramData").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"));
    base.join("SMP")
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
