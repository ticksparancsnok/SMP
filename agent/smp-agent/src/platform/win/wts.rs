//! Windows sessions ("WTS" = Windows Terminal Services, the API's old name).
//!
//! Session 0 belongs to services. Each logged-in user (or the login screen) has its
//! own session: 1, 2, ... The physical screen and keyboard are the "console"
//! session; remote desktop users get other sessions.

use std::os::windows::io::{FromRawHandle, OwnedHandle};
use std::path::Path;

use anyhow::{Context, Result};
use windows::core::{HSTRING, PWSTR};
use windows::Win32::Foundation::{CloseHandle, HANDLE};
use windows::Win32::Security::{
    DuplicateTokenEx, SecurityIdentification, SetTokenInformation, TokenPrimary, TokenSessionId, TOKEN_ALL_ACCESS,
    TOKEN_ADJUST_DEFAULT, TOKEN_ADJUST_SESSIONID, TOKEN_ASSIGN_PRIMARY, TOKEN_DUPLICATE, TOKEN_QUERY,
};
use windows::Win32::System::RemoteDesktop::{
    ProcessIdToSessionId, WTSActive, WTSEnumerateSessionsW, WTSFreeMemory, WTSGetActiveConsoleSessionId,
    WTSQuerySessionInformationW, WTSUserName, WTS_CURRENT_SERVER_HANDLE, WTS_SESSION_INFOW,
};
use windows::Win32::System::Threading::{
    CreateProcessAsUserW, GetCurrentProcess, GetCurrentProcessId, OpenProcessToken, CREATE_NO_WINDOW, PROCESS_INFORMATION,
    STARTUPINFOW,
};

/// The session whose screen we should show: the console if someone is logged in
/// there, else an active remote desktop session with a user (labs used over RDP,
/// virtual machines), else the console's login screen. None: no session at all.
pub fn interactive_session() -> Option<u32> {
    // SAFETY: no arguments; returns 0xFFFFFFFF when there is no console session.
    let console = unsafe { WTSGetActiveConsoleSessionId() };
    if console != u32::MAX && session_user(console).is_some() {
        return Some(console);
    }
    let remote = active_sessions().into_iter().find(|&id| id != 0 && id != console && session_user(id).is_some());
    remote.or((console != u32::MAX && console != 0).then_some(console))
}

fn active_sessions() -> Vec<u32> {
    let mut list: *mut WTS_SESSION_INFOW = std::ptr::null_mut();
    let mut count = 0u32;
    // SAFETY: Windows allocates the list; we copy what we need and free it.
    unsafe {
        if WTSEnumerateSessionsW(Some(WTS_CURRENT_SERVER_HANDLE), 0, 1, &mut list, &mut count).is_err() {
            return vec![];
        }
        let ids = std::slice::from_raw_parts(list, count as usize)
            .iter()
            .filter(|s| s.State == WTSActive)
            .map(|s| s.SessionId)
            .collect();
        WTSFreeMemory(list.cast());
        ids
    }
}

/// The user name logged in to a session (None: nobody, e.g. the login screen).
pub fn session_user(session: u32) -> Option<String> {
    let mut buf = PWSTR::null();
    let mut bytes = 0u32;
    // SAFETY: Windows allocates the string; we copy it and free it.
    unsafe {
        WTSQuerySessionInformationW(Some(WTS_CURRENT_SERVER_HANDLE), session, WTSUserName, &mut buf, &mut bytes).ok()?;
        let name = buf.to_string().ok();
        WTSFreeMemory(buf.0.cast());
        name.filter(|n| !n.is_empty())
    }
}

/// The session this process runs in (0 = we are a service).
pub fn current_session() -> u32 {
    let mut session = 0;
    // SAFETY: plain out-parameter.
    let _ = unsafe { ProcessIdToSessionId(GetCurrentProcessId(), &mut session) };
    session
}

/// Start `exe` in another session, as SYSTEM (like this service), on the user's
/// desktop. Only the service (SYSTEM) can do this. Returns the process handle and id.
///
/// Why SYSTEM and not as the logged-in user: a student could kill or tamper with a
/// process running under their own account (e.g. to show the teacher a fake
/// screen). They can't touch a SYSTEM process.
pub fn spawn_in_session(session: u32, exe: &Path, command_line: &str) -> Result<(OwnedHandle, u32)> {
    // SAFETY: every handle is closed on every path; buffers outlive the calls.
    unsafe {
        let mut own = HANDLE::default();
        OpenProcessToken(
            GetCurrentProcess(),
            TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ADJUST_SESSIONID | TOKEN_ADJUST_DEFAULT | TOKEN_ASSIGN_PRIMARY,
            &mut own,
        )
        .context("opening our token")?;
        let mut token = HANDLE::default();
        let duplicated = DuplicateTokenEx(own, TOKEN_ALL_ACCESS, None, SecurityIdentification, TokenPrimary, &mut token);
        let _ = CloseHandle(own);
        duplicated.context("copying our token")?;

        let result = (|| {
            SetTokenInformation(token, TokenSessionId, (&session as *const u32).cast(), 4)
                .context("moving the token to the user's session")?;
            let mut desktop: Vec<u16> = "winsta0\\default\0".encode_utf16().collect();
            let startup = STARTUPINFOW {
                cb: std::mem::size_of::<STARTUPINFOW>() as u32,
                lpDesktop: PWSTR(desktop.as_mut_ptr()),
                ..Default::default()
            };
            let mut command: Vec<u16> = command_line.encode_utf16().chain([0]).collect();
            let mut process = PROCESS_INFORMATION::default();
            CreateProcessAsUserW(
                Some(token),
                &HSTRING::from(exe.as_os_str()),
                Some(PWSTR(command.as_mut_ptr())),
                None,
                None,
                false,
                CREATE_NO_WINDOW,
                None,
                None,
                &startup,
                &mut process,
            )
            .context("starting the session helper")?;
            let _ = CloseHandle(process.hThread);
            Ok((OwnedHandle::from_raw_handle(process.hProcess.0), process.dwProcessId))
        })();
        let _ = CloseHandle(token);
        result
    }
}
