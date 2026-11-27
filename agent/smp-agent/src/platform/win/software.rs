// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Installing software on Windows (what to do is decided in capabilities/software/).
//!
//!   detection   .msi: Windows Installer's own record of the product code
//!               (MsiQueryProductStateW); registry: HKLM, 64-bit view first, then the
//!               32-bit one (32-bit installers write to WOW6432Node); file: exists.
//!   running     CreateProcess (Rust's Command) without a window, as SYSTEM in session
//!               0, never through cmd.exe: the admin's arguments reach the installer
//!               as typed (`raw_arg`) and can't chain other commands. An installer that
//!               shows a dialog anyway would wait for a click nobody can give: the
//!               package's timeout stops it.
//!   restarting  shutdown.exe /r /t <seconds> /c <message>: Windows itself warns the
//!               logged-on user with a countdown and the message.

use std::os::windows::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use windows::core::{HSTRING, PCWSTR, PWSTR};
use windows::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_SUCCESS};
use windows::Win32::System::ApplicationInstallationAndServicing::{
    MsiCloseHandle, MsiDatabaseOpenViewW, MsiOpenDatabaseW, MsiQueryProductStateW, MsiRecordGetStringW,
    MsiViewExecute, MsiViewFetch, INSTALLSTATE_DEFAULT, MSIDBOPEN_READONLY, MSIHANDLE,
};
use windows::Win32::System::Environment::ExpandEnvironmentStringsW;
use windows::Win32::System::Registry::{
    RegCloseKey, RegGetValueW, RegOpenKeyExW, HKEY, HKEY_LOCAL_MACHINE, KEY_READ, KEY_WOW64_32KEY, KEY_WOW64_64KEY,
    REG_DWORD, REG_EXPAND_SZ, REG_QWORD, REG_SZ, REG_VALUE_TYPE, RRF_NOEXPAND, RRF_RT_ANY,
};
use windows::Win32::System::SystemInformation::GetTickCount64;

use crate::capabilities::software::config::{Detection, Package};
use crate::capabilities::software::plan::Invocation;
use crate::capabilities::software::Installer;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub struct WindowsInstaller;

impl Installer for WindowsInstaller {
    fn is_installed(&self, detection: &Detection, product_code: Option<&str>) -> Result<bool> {
        match detection {
            Detection::Msi { product_code: own } => {
                let code = own.as_deref().or(product_code).context("no product code to look for")?;
                // SAFETY: a plain query with a NUL-terminated string we own.
                let state = unsafe { MsiQueryProductStateW(&HSTRING::from(code)) };
                Ok(state == INSTALLSTATE_DEFAULT)
            }
            Detection::Registry { key, value, equals } => registry_matches(key, value.as_deref(), equals.as_deref()),
            Detection::File { path } => Ok(Path::new(&expand(path)).exists()),
        }
    }

    fn msi_product_code(&self, msi: &Path) -> Result<String> {
        msi_property(msi, "ProductCode")
    }

    fn run(&self, package: &Package, invocation: &Invocation, timeout: Duration) -> Result<Option<i32>> {
        let program = expand(&invocation.program);
        let mut command = Command::new(&program);
        command.args(&invocation.args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        if !invocation.raw.is_empty() {
            command.raw_arg(&invocation.raw); // exactly as the admin typed it (see the top)
        }
        command.creation_flags(CREATE_NO_WINDOW);
        let mut child = command.spawn().with_context(|| format!("starting {program}"))?;
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(status) = child.try_wait()? {
                return Ok(Some(status.code().unwrap_or(-1)));
            }
            if Instant::now() > deadline {
                log::warn!("software: {} ran longer than {} s; stopping it", package.label(), timeout.as_secs());
                let _ = child.kill();
                let _ = child.wait();
                return Ok(None);
            }
            std::thread::sleep(Duration::from_millis(500));
        }
    }

    fn boot_time(&self) -> Option<u64> {
        // SAFETY: plain query (milliseconds since Windows started).
        let uptime_s = unsafe { GetTickCount64() } / 1000;
        let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        Some(now.saturating_sub(uptime_s))
    }

    fn schedule_restart(&self, after: Duration, programs: &[String]) -> Result<()> {
        let minutes = after.as_secs().div_ceil(60);
        let list = programs.join(", ");
        let message = if super::system_language_is_hungarian() {
            format!("Az SMP programot telepített ({list}). A számítógép {minutes} perc múlva újraindul: mentse a munkáját!")
        } else {
            format!("SMP installed software ({list}). This computer restarts in {minutes} minute(s): please save your work.")
        };
        // shutdown.exe allows at most 512 characters of comment.
        let message: String = message.chars().take(500).collect();
        let status = Command::new(expand(r"%SystemRoot%\System32\shutdown.exe"))
            // /d p:4:1 = planned, "Application: Maintenance" (shows in the event log)
            .args(["/r", "/t", &after.as_secs().to_string(), "/d", "p:4:1", "/c", &message])
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .context("starting shutdown.exe")?;
        match status.code() {
            Some(0) => Ok(()),
            Some(1190) => Ok(()), // ERROR_SHUTDOWN_IS_SCHEDULED: a restart is already coming
            other => bail!("shutdown.exe ended with {other:?}"),
        }
    }
}

/// `%ProgramFiles%\X` -> `C:\Program Files\X` (the service's, i.e. SYSTEM's, variables).
fn expand(text: &str) -> String {
    let source = HSTRING::from(text);
    // SAFETY: first call asks for the size, the second fills a buffer of that size.
    unsafe {
        let needed = ExpandEnvironmentStringsW(&source, None);
        if needed == 0 {
            return text.to_string();
        }
        let mut buf = vec![0u16; needed as usize];
        let written = ExpandEnvironmentStringsW(&source, Some(&mut buf));
        if written == 0 || written > needed {
            return text.to_string();
        }
        String::from_utf16_lossy(&buf[..written as usize - 1]) // without the NUL
    }
}

/// "HKLM\SOFTWARE\X" or "HKEY_LOCAL_MACHINE\SOFTWARE\X" -> "SOFTWARE\X".
fn hklm_subkey(key: &str) -> Option<&str> {
    let upper = key.to_ascii_uppercase();
    ["HKLM\\", "HKEY_LOCAL_MACHINE\\"].iter().find(|p| upper.starts_with(*p)).map(|p| &key[p.len()..])
}

/// The rule matches in the 64-bit or the 32-bit registry view.
fn registry_matches(key: &str, value: Option<&str>, equals: Option<&str>) -> Result<bool> {
    let subkey = hklm_subkey(key).context("only keys under HKLM\\ can be checked")?;
    for view in [KEY_WOW64_64KEY, KEY_WOW64_32KEY] {
        let mut hkey = HKEY::default();
        // SAFETY: hkey is a valid out pointer; the key is closed below.
        let opened = unsafe { RegOpenKeyExW(HKEY_LOCAL_MACHINE, &HSTRING::from(subkey), None, KEY_READ | view, &mut hkey) };
        if opened == ERROR_FILE_NOT_FOUND {
            continue;
        }
        if opened != ERROR_SUCCESS {
            bail!("can't open {key}: error {}", opened.0);
        }
        let found = match value {
            None => Some(true),
            Some(name) => read_value(hkey, name).map(|data| equals.is_none_or(|want| data.trim() == want.trim())),
        };
        // SAFETY: hkey was opened above.
        unsafe {
            let _ = RegCloseKey(hkey);
        }
        if found == Some(true) {
            return Ok(true);
        }
    }
    Ok(false)
}

/// A value's data as text (strings as they are, numbers in decimal), or None if the
/// value doesn't exist (or is of another type).
fn read_value(hkey: HKEY, name: &str) -> Option<String> {
    let mut kind = REG_VALUE_TYPE::default();
    let mut buf = vec![0u8; 4096];
    let mut size = buf.len() as u32;
    // SAFETY: buf/size describe a buffer we own; kind is a valid out pointer.
    let read = unsafe {
        RegGetValueW(
            hkey,
            PCWSTR::null(),
            &HSTRING::from(name),
            RRF_RT_ANY | RRF_NOEXPAND,
            Some(&mut kind),
            Some(buf.as_mut_ptr().cast()),
            Some(&mut size),
        )
    };
    if read != ERROR_SUCCESS {
        return None;
    }
    let data = &buf[..size as usize];
    match kind {
        REG_SZ | REG_EXPAND_SZ => {
            let wide: Vec<u16> = data.as_chunks::<2>().0.iter().map(|&pair| u16::from_le_bytes(pair)).collect();
            Some(String::from_utf16_lossy(&wide).trim_end_matches('\0').to_string())
        }
        REG_DWORD if data.len() >= 4 => Some(u32::from_le_bytes(data[..4].try_into().ok()?).to_string()),
        REG_QWORD if data.len() >= 8 => Some(u64::from_le_bytes(data[..8].try_into().ok()?).to_string()),
        _ => None,
    }
}

/// Closes an MSI handle when it goes out of scope (also on early returns).
struct MsiHandle(MSIHANDLE);

impl Drop for MsiHandle {
    fn drop(&mut self) {
        if self.0 .0 != 0 {
            // SAFETY: a handle MSI gave us, closed once.
            unsafe {
                MsiCloseHandle(self.0);
            }
        }
    }
}

/// A property of an .msi file (its Property table), e.g. ProductCode.
fn msi_property(msi: &Path, property: &str) -> Result<String> {
    let check = |what: &str, code: u32| if code == 0 { Ok(()) } else { Err(anyhow::anyhow!("{what}: error {code}")) };
    let (mut db, mut view, mut record) = (MsiHandle(MSIHANDLE(0)), MsiHandle(MSIHANDLE(0)), MsiHandle(MSIHANDLE(0)));
    // `property` is ours (a constant), not the admin's: safe to put in the query.
    let query = format!("SELECT `Value` FROM `Property` WHERE `Property` = '{property}'");
    // SAFETY: each call gets valid strings and out pointers; the handles close on drop.
    unsafe {
        check("opening the .msi", MsiOpenDatabaseW(&HSTRING::from(msi.as_os_str()), MSIDBOPEN_READONLY, &mut db.0))?;
        check("reading its properties", MsiDatabaseOpenViewW(db.0, &HSTRING::from(query), &mut view.0))?;
        check("reading its properties", MsiViewExecute(view.0, MSIHANDLE(0)))?;
        check(&format!("finding {property}"), MsiViewFetch(view.0, &mut record.0))?;
        let mut buf = vec![0u16; 128];
        let mut len = buf.len() as u32;
        check("reading the value", MsiRecordGetStringW(record.0, 1, Some(PWSTR(buf.as_mut_ptr())), Some(&mut len)))?;
        Ok(String::from_utf16_lossy(&buf[..len as usize]))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hklm_prefixes() {
        assert_eq!(hklm_subkey(r"HKLM\SOFTWARE\X"), Some(r"SOFTWARE\X"));
        assert_eq!(hklm_subkey(r"hkey_local_machine\SOFTWARE\X"), Some(r"SOFTWARE\X"));
        assert_eq!(hklm_subkey(r"HKCU\SOFTWARE\X"), None);
    }
}
