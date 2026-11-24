// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! The service's side of the session helper (see mod.rs for why there is one).
//!
//! A manager thread checks every 2 seconds which session is in front of the screen
//! (wts.rs) and keeps exactly one helper running there: it starts one after logon,
//! and replaces it when the user logs off, switches, or the helper dies. Then it
//! tells the new helper the current state: the indicator (so the tray icon is right)
//! and the screen lock (so logging off and on again doesn't unlock the computer).
//!
//! Two capabilities use the helper: screen monitoring (pictures, the indicator) and
//! the screen lock. Everything that talks to the helper holds one mutex (`Inner`),
//! so requests never mix on the pipe, and a new helper gets the latest state before
//! anyone else can send it something.
//!
//! The pipe is private: a new random name each time, only SYSTEM and administrators
//! may open it, at most one client, and we check that the client is the very
//! process we started (by process id).

use std::fs::File;
use std::io::BufReader;
use std::os::windows::io::{AsRawHandle, FromRawHandle, IntoRawHandle, OwnedHandle};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use windows::core::HSTRING;
use windows::Win32::Foundation::{LocalFree, ERROR_PIPE_CONNECTED, HANDLE, HLOCAL, WAIT_TIMEOUT};
use windows::Win32::Security::Authorization::{ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1};
use windows::Win32::Security::{PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES};
use windows::Win32::Storage::FileSystem::{FILE_FLAG_FIRST_PIPE_INSTANCE, PIPE_ACCESS_DUPLEX};
use windows::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, GetNamedPipeClientProcessId, PIPE_READMODE_BYTE, PIPE_REJECT_REMOTE_CLIENTS,
    PIPE_TYPE_BYTE, PIPE_WAIT,
};
use windows::Win32::System::Threading::{TerminateProcess, WaitForSingleObject};

use super::wts;
use crate::capabilities::lock::{LockScreen, LockTarget};
use crate::capabilities::screen::encode::Picture;
use crate::capabilities::screen::source::ScreenSource;
use crate::platform::helper_protocol::{self as protocol, Request};

/// Only SYSTEM (SY) and administrators (BA) may open the pipe.
const PIPE_ACL: &str = "D:P(A;;GA;;;SY)(A;;GA;;;BA)";
const CHECK_EVERY: Duration = Duration::from_secs(2);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);

/// The open pipe to a running helper.
struct Connection {
    reader: BufReader<File>,
    writer: File,
}

impl Connection {
    fn ask_picture(&mut self, max_width: u32, quality: u8) -> std::io::Result<Result<Picture, String>> {
        protocol::write_request(&mut self.writer, &Request::Capture { max_width, quality })?;
        protocol::read_picture(&mut self.reader)
    }

    fn set_indicator(&mut self, monitoring: bool) -> std::io::Result<()> {
        protocol::write_request(&mut self.writer, &Request::Indicator { monitoring })?;
        protocol::read_ok(&mut self.reader)
    }

    fn set_lock(&mut self, lock: Option<&LockScreen>) -> std::io::Result<()> {
        let request = match lock {
            // an empty logo: the helper shows SMP's (saves sending it each time)
            Some(l) => Request::Lock { text: l.message.clone(), png: l.logo.as_deref().cloned().unwrap_or_default() },
            None => Request::Unlock,
        };
        protocol::write_request(&mut self.writer, &request)?;
        protocol::read_ok(&mut self.reader)
    }

    /// Bring a new helper up to date.
    fn send_state(&mut self, inner: &Inner) -> std::io::Result<()> {
        self.set_indicator(inner.monitoring)?;
        self.set_lock(inner.lock.as_ref())
    }
}

/// What the capabilities want the helper to show, and the pipe to it.
struct Inner {
    connection: Option<Connection>,
    monitoring: bool,
    lock: Option<LockScreen>,
}

/// State shared by the manager thread and the capabilities.
struct Shared {
    inner: Mutex<Inner>,
}

impl Shared {
    fn inner(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap()
    }
}

/// What the screen and lock capabilities hold: a `ScreenSource` and a `LockTarget`
/// backed by the helper.
pub struct HelperLink {
    shared: Arc<Shared>,
}

/// Start the manager thread (once) and return a link to the helper.
pub fn start() -> HelperLink {
    static SHARED: OnceLock<Arc<Shared>> = OnceLock::new();
    let shared = SHARED.get_or_init(|| {
        let shared = Arc::new(Shared { inner: Mutex::new(Inner { connection: None, monitoring: false, lock: None }) });
        let for_thread = shared.clone();
        std::thread::Builder::new()
            .name("session-helper".into())
            .spawn(move || manage(&for_thread))
            .expect("starting the session manager thread");
        shared
    });
    HelperLink { shared: shared.clone() }
}

impl ScreenSource for HelperLink {
    fn capture(&mut self, max_width: u32, quality: u8) -> Result<Picture> {
        let mut inner = self.shared.inner();
        let link = inner.connection.as_mut().context("no user session to capture yet (login screen?)")?;
        match link.ask_picture(max_width, quality) {
            Ok(Ok(picture)) => Ok(picture),
            Ok(Err(message)) => Err(anyhow!("the screen can't be captured now: {message}")),
            Err(e) => {
                inner.connection = None; // the manager restarts the helper
                Err(e).context("lost the session helper")
            }
        }
    }

    fn set_indicator(&mut self, monitoring: bool) {
        let mut inner = self.shared.inner();
        inner.monitoring = monitoring;
        if let Some(link) = inner.connection.as_mut() {
            if let Err(e) = link.set_indicator(monitoring) {
                log::warn!("couldn't update the tray icon: {e}");
                inner.connection = None;
            }
        }
        // No helper right now: the manager sends the state when it starts one.
    }
}

impl LockTarget for HelperLink {
    fn set_lock(&mut self, lock: Option<&LockScreen>) {
        let mut inner = self.shared.inner();
        inner.lock = lock.cloned();
        if let Some(link) = inner.connection.as_mut() {
            if let Err(e) = link.set_lock(lock) {
                log::warn!("couldn't {} the screen: {e}", if lock.is_some() { "lock" } else { "unlock" });
                inner.connection = None; // the manager starts a new helper and sends it the lock
            }
        }
        // No helper right now (login screen): the manager sends the lock when it starts one.
    }
}

/// A helper process we started.
struct Helper {
    process: OwnedHandle,
    session: u32,
}

impl Helper {
    fn is_running(&self) -> bool {
        // SAFETY: a valid process handle; 0 ms = just check.
        unsafe { WaitForSingleObject(HANDLE(self.process.as_raw_handle()), 0) == WAIT_TIMEOUT }
    }

    fn kill(&self) {
        // SAFETY: a valid process handle with terminate rights (we created it).
        let _ = unsafe { TerminateProcess(HANDLE(self.process.as_raw_handle()), 0) };
    }
}

fn manage(shared: &Shared) {
    let mut helper: Option<Helper> = None;
    let mut last_error = String::new();
    loop {
        let wanted = wts::interactive_session();
        if let Some(h) = &helper {
            let lost_link = shared.inner().connection.is_none();
            if !h.is_running() || lost_link || Some(h.session) != wanted {
                log::info!("session helper in session {} stopped or no longer needed", h.session);
                h.kill();
                shared.inner().connection = None;
                helper = None;
            }
        }
        if let (None, Some(session)) = (&helper, wanted) {
            match launch(session) {
                Ok((h, mut connection)) => {
                    // Under the lock: nobody changes the state while the new helper
                    // learns it, so it can't miss a change.
                    let mut inner = shared.inner();
                    if connection.send_state(&inner).is_ok() {
                        log::info!("session helper running in session {session}");
                        inner.connection = Some(connection);
                        helper = Some(h);
                        last_error.clear();
                    } else {
                        h.kill();
                    }
                }
                Err(e) => {
                    let e = format!("{e:#}");
                    if e != last_error {
                        log::warn!("couldn't start the session helper in session {session}: {e}");
                        last_error = e;
                    }
                }
            }
        }
        std::thread::sleep(CHECK_EVERY);
    }
}

/// Create the pipe, start the helper in `session`, wait until it connected.
fn launch(session: u32) -> Result<(Helper, Connection)> {
    let name = format!(r"\\.\pipe\smp-agent-{:016x}", rand_u64());
    let pipe = create_pipe(&name)?;
    let exe = std::env::current_exe().context("finding our own exe")?;
    let args = ["session-helper", "--pipe", &name, "--server-pid", &std::process::id().to_string()];

    let (process, pid) = if wts::current_session() == 0 {
        // the normal case: we are the service, the helper goes to the user's session
        let command_line = format!("\"{}\" {}", exe.display(), args.join(" "));
        wts::spawn_in_session(session, &exe, &command_line)?
    } else {
        // `smp-agent run` from an (admin) command prompt: same session as us
        let child = std::process::Command::new(&exe).args(args).spawn().context("starting the session helper")?;
        let pid = child.id();
        // SAFETY: we take over the process handle the Child owned.
        (unsafe { OwnedHandle::from_raw_handle(child.into_raw_handle()) }, pid)
    };
    let helper = Helper { process, session };

    match wait_for_client(&pipe, &name, pid) {
        Ok(()) => {
            let file = File::from(pipe);
            let reader = BufReader::new(file.try_clone().context("pipe handle")?);
            Ok((helper, Connection { reader, writer: file }))
        }
        Err(e) => {
            helper.kill();
            Err(e)
        }
    }
}

/// Block until a client connects, then make sure it is our helper (`pid`).
/// If nobody comes within CONNECT_TIMEOUT, we connect to it ourselves to wake up the
/// blocked call, and the pid check then fails.
fn wait_for_client(pipe: &OwnedHandle, name: &str, pid: u32) -> Result<()> {
    let connected = Arc::new(AtomicBool::new(false));
    let watchdog = {
        let (connected, name) = (connected.clone(), name.to_string());
        std::thread::spawn(move || {
            std::thread::sleep(CONNECT_TIMEOUT);
            if !connected.load(Ordering::SeqCst) {
                let _ = std::fs::OpenOptions::new().read(true).write(true).open(&name);
            }
        })
    };
    let handle = HANDLE(pipe.as_raw_handle());
    // SAFETY: a valid pipe handle; no OVERLAPPED = a plain blocking wait.
    let result = unsafe { ConnectNamedPipe(handle, None) };
    connected.store(true, Ordering::SeqCst);
    drop(watchdog); // it finishes on its own
    if let Err(e) = result {
        if e.code() != ERROR_PIPE_CONNECTED.to_hresult() {
            return Err(e).context("waiting for the session helper");
        }
    }
    let mut client = 0u32;
    // SAFETY: plain out-parameter.
    unsafe { GetNamedPipeClientProcessId(handle, &mut client) }.context("who connected")?;
    if client != pid {
        bail!("the session helper didn't connect (process {client} came instead of {pid})");
    }
    Ok(())
}

fn create_pipe(name: &str) -> Result<OwnedHandle> {
    let mut sd = PSECURITY_DESCRIPTOR::default();
    // SAFETY: Windows allocates the security descriptor; freed below.
    unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(&HSTRING::from(PIPE_ACL), SDDL_REVISION_1, &mut sd, None)
            .context("building the pipe ACL")?;
        let attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: sd.0,
            bInheritHandle: false.into(),
        };
        let pipe = CreateNamedPipeW(
            &HSTRING::from(name),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            1 << 20,
            4096,
            0,
            Some(&attributes),
        );
        let _ = LocalFree(Some(HLOCAL(sd.0)));
        if pipe.is_invalid() {
            return Err(std::io::Error::last_os_error()).context("creating the helper pipe");
        }
        Ok(OwnedHandle::from_raw_handle(pipe.0))
    }
}

/// A random number for the pipe name (so nobody can create it before us).
fn rand_u64() -> u64 {
    use rand_core::{OsRng, RngCore};
    OsRng.next_u64()
}
