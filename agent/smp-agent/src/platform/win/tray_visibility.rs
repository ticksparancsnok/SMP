//! Keeping the SMP tray icon on the taskbar, not hidden in the "^" overflow.
//!
//! The icon is the GDPR notice, so it must be visible without clicking anything.
//! Windows hides new tray icons by default, differently per version:
//!
//!   Windows 11  Explorer keeps a registry entry per tray icon and user:
//!               HKCU\Control Panel\NotifyIconSettings\<number>, with the program's
//!               path (ExecutablePath) and IsPromoted (1 = shown on the taskbar).
//!               The helper finds ours in the logged-in user's registry and sets
//!               IsPromoted = 1, and checks again every 30 s, so turning it off in
//!               Settings doesn't stick.
//!   Windows 10  No per-icon switch Explorer picks up. There is a documented machine
//!               policy, "Turn off notification area cleanup" (NoAutoTrayNotify = 1),
//!               which shows every tray icon. The service sets it unless the school's
//!               own policy already decides; it applies from the next logon.
//!               Uninstalling removes it again, if we were the ones who set it.

use std::path::Path;
use std::time::Duration;

use windows::core::{HSTRING, PWSTR};
use windows::Win32::Foundation::{CloseHandle, LocalFree, ERROR_SUCCESS, HANDLE, HLOCAL};
use windows::Win32::Security::Authorization::ConvertSidToStringSidW;
use windows::Win32::Security::{GetTokenInformation, TokenUser, TOKEN_USER};
use windows::Win32::System::Registry::{
    RegCloseKey, RegDeleteKeyValueW, RegEnumKeyExW, RegGetValueW, RegOpenKeyExW, RegSetKeyValueW, HKEY,
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, HKEY_USERS, KEY_READ, REG_DWORD, RRF_RT_REG_DWORD, RRF_RT_REG_SZ,
};
use windows::Win32::System::RemoteDesktop::WTSQueryUserToken;

use super::tray::TrayNotifier;
use super::wts;

/// Windows 11 starts at build 22000.
fn is_windows_11() -> bool {
    super::windows_build() >= 22000
}

// ------------------------------------------------------------------ Windows 11

/// Start a thread that keeps our icon promoted (Windows 11 only; does nothing on 10).
/// Explorer creates our entry only after the icon first appeared, so the first
/// minute it looks every 5 s, then every 30 s.
pub fn keep_promoted(tray: TrayNotifier) {
    if !is_windows_11() {
        return;
    }
    let Ok(exe) = std::env::current_exe() else { return };
    let _ = std::thread::Builder::new().name("tray-visibility".into()).spawn(move || {
        let user = UserHive::of_this_session();
        let mut round = 0u32; // runs as long as the helper does
        loop {
            match promote(&user, &exe) {
                Ok(true) => {
                    log::info!("tray icon set to always visible");
                    tray.refresh(); // re-add the icon so Explorer shows it at once
                }
                Ok(false) => {}
                Err(e) if round == 12 => log::warn!("couldn't make the tray icon always visible: {e}"),
                Err(_) => {}
            }
            std::thread::sleep(Duration::from_secs(if round < 12 { 5 } else { 30 }));
            round = round.saturating_add(1);
        }
    });
}

/// The registry of the user in front of the screen. The helper runs as SYSTEM, whose
/// own HKEY_CURRENT_USER is not the user's: we go through HKEY_USERS\<user's SID>.
struct UserHive {
    root: HKEY,
    prefix: String, // "<SID>\" under HKEY_USERS, or "" for HKEY_CURRENT_USER
}

impl UserHive {
    fn of_this_session() -> Self {
        match session_user_sid(wts::current_session()) {
            Some(sid) => Self { root: HKEY_USERS, prefix: format!("{sid}\\") },
            // `smp-agent run` by hand: the helper runs as the user, HKCU is right
            None => Self { root: HKEY_CURRENT_USER, prefix: String::new() },
        }
    }
}

/// Set IsPromoted = 1 on our icon's entry. Ok(true) if something changed.
fn promote(user: &UserHive, exe: &Path) -> Result<bool, String> {
    const ICONS: &str = r"Control Panel\NotifyIconSettings";
    let parent = format!("{}{ICONS}", user.prefix);
    let mut found = false;
    let mut changed = false;
    for id in subkeys(user.root, &parent)? {
        let entry = format!("{parent}\\{id}");
        let Some(path) = get_string(user.root, &entry, "ExecutablePath") else { continue };
        if !same_program(&path, exe) {
            continue;
        }
        found = true;
        if get_dword(user.root, &entry, "IsPromoted") != Some(1) {
            set_dword(user.root, &entry, "IsPromoted", 1)?;
            changed = true;
        }
    }
    if found { Ok(changed) } else { Err("Explorer has no entry for the icon yet".into()) }
}

/// Explorer may write the path with a folder GUID instead of "C:\Program Files"
/// ("{6D809377-...}\SMP\smp-agent.exe"), so compare the folder and file name only.
fn same_program(registered: &str, exe: &Path) -> bool {
    let tail = |p: &Path| -> Option<String> {
        let file = p.file_name()?.to_string_lossy().to_lowercase();
        let dir = p.parent()?.file_name()?.to_string_lossy().to_lowercase();
        Some(format!("{dir}\\{file}"))
    };
    match tail(exe) {
        Some(ours) => registered.to_lowercase().replace('/', "\\").ends_with(&format!("\\{ours}")),
        None => false,
    }
}

/// "S-1-5-21-..." of the user logged in to `session` (needs SYSTEM).
fn session_user_sid(session: u32) -> Option<String> {
    // SAFETY: the token is closed below; the buffer is big enough (we ask for the size first).
    unsafe {
        let mut token = HANDLE::default();
        WTSQueryUserToken(session, &mut token).ok()?;
        let mut size = 0u32;
        let _ = GetTokenInformation(token, TokenUser, None, 0, &mut size);
        let mut buf = vec![0u8; size as usize];
        let got = GetTokenInformation(token, TokenUser, Some(buf.as_mut_ptr().cast()), size, &mut size);
        let _ = CloseHandle(token);
        got.ok()?;
        let user = &*(buf.as_ptr() as *const TOKEN_USER);
        let mut text = PWSTR::null();
        ConvertSidToStringSidW(user.User.Sid, &mut text).ok()?;
        let sid = text.to_string().ok();
        let _ = LocalFree(Some(HLOCAL(text.0.cast())));
        sid
    }
}

// ------------------------------------------------------------------ Windows 10

const EXPLORER_POLICY: &str = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer";
const SHOW_ALL_ICONS: &str = "NoAutoTrayNotify";
/// Where we remember that the policy value is ours (not the school's GPO).
const OUR_KEY: &str = r"SOFTWARE\SMP";
const OUR_MARK: &str = "SetNoAutoTrayNotify";

/// Windows 10: "Turn off notification area cleanup" for this computer, unless that
/// policy is already set (then the school's IT decided; we leave it alone).
pub fn ensure_windows10_policy() {
    if is_windows_11() {
        return;
    }
    if get_dword(HKEY_LOCAL_MACHINE, EXPLORER_POLICY, SHOW_ALL_ICONS).is_some() {
        return; // set before: by us earlier, or by the school's policy
    }
    match set_dword(HKEY_LOCAL_MACHINE, EXPLORER_POLICY, SHOW_ALL_ICONS, 1)
        .and_then(|_| set_dword(HKEY_LOCAL_MACHINE, OUR_KEY, OUR_MARK, 1))
    {
        Ok(()) => log::info!("Windows 10: all tray icons are shown from the next logon (NoAutoTrayNotify)"),
        Err(e) => log::warn!("couldn't set the tray icon policy: {e}"),
    }
}

/// Uninstalling: take the policy back if we set it.
pub fn remove_windows10_policy() {
    if get_dword(HKEY_LOCAL_MACHINE, OUR_KEY, OUR_MARK) == Some(1) {
        // SAFETY: plain registry calls with valid strings.
        unsafe {
            let _ = RegDeleteKeyValueW(HKEY_LOCAL_MACHINE, &HSTRING::from(EXPLORER_POLICY), &HSTRING::from(SHOW_ALL_ICONS));
            let _ = RegDeleteKeyValueW(HKEY_LOCAL_MACHINE, &HSTRING::from(OUR_KEY), &HSTRING::from(OUR_MARK));
        }
    }
}

// ------------------------------------------------------------------ small registry helpers

fn subkeys(root: HKEY, path: &str) -> Result<Vec<String>, String> {
    let mut key = HKEY::default();
    // SAFETY: the key is closed before returning; name buffers are ours.
    unsafe {
        let err = RegOpenKeyExW(root, &HSTRING::from(path), None, KEY_READ, &mut key);
        if err != ERROR_SUCCESS {
            return Err(format!("can't open {path} ({})", err.0));
        }
        let mut names = vec![];
        for index in 0.. {
            let mut name = [0u16; 256];
            let mut len = name.len() as u32;
            let err = RegEnumKeyExW(key, index, Some(PWSTR(name.as_mut_ptr())), &mut len, None, None, None, None);
            if err != ERROR_SUCCESS {
                break; // ERROR_NO_MORE_ITEMS
            }
            names.push(String::from_utf16_lossy(&name[..len as usize]));
        }
        let _ = RegCloseKey(key);
        Ok(names)
    }
}

fn get_string(root: HKEY, path: &str, name: &str) -> Option<String> {
    let mut buf = [0u16; 1024];
    let mut size = std::mem::size_of_val(&buf) as u32;
    // SAFETY: buf and size describe our buffer.
    let err = unsafe {
        RegGetValueW(root, &HSTRING::from(path), &HSTRING::from(name), RRF_RT_REG_SZ, None,
                     Some(buf.as_mut_ptr().cast()), Some(&mut size))
    };
    (err == ERROR_SUCCESS).then(|| String::from_utf16_lossy(&buf[..(size as usize / 2).saturating_sub(1)]))
}

fn get_dword(root: HKEY, path: &str, name: &str) -> Option<u32> {
    let (mut value, mut size) = (0u32, 4u32);
    // SAFETY: value is a u32 and size says so.
    let err = unsafe {
        RegGetValueW(root, &HSTRING::from(path), &HSTRING::from(name), RRF_RT_REG_DWORD, None,
                     Some((&mut value as *mut u32).cast()), Some(&mut size))
    };
    (err == ERROR_SUCCESS).then_some(value)
}

/// Creates the key if it doesn't exist.
fn set_dword(root: HKEY, path: &str, name: &str, value: u32) -> Result<(), String> {
    // SAFETY: we pass a pointer to a u32 and its size.
    let err = unsafe {
        RegSetKeyValueW(root, &HSTRING::from(path), &HSTRING::from(name), REG_DWORD.0,
                        Some((&value as *const u32).cast()), 4)
    };
    if err == ERROR_SUCCESS { Ok(()) } else { Err(format!("writing {path}\\{name} ({})", err.0)) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognises_our_program() {
        let exe = Path::new(r"C:\Program Files\SMP\smp-agent.exe");
        assert!(same_program(r"{6D809377-6AF0-444B-8957-A3773F02200E}\SMP\smp-agent.exe", exe));
        assert!(same_program(r"C:\PROGRAM FILES\smp\SMP-AGENT.EXE", exe));
        assert!(!same_program(r"C:\Program Files\Other\smp-agent.exe", exe));
        assert!(!same_program(r"C:\Windows\explorer.exe", exe));
    }
}
