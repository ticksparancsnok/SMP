// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Blocking keyboard and mouse: everything while the screen is locked, or only the
//! student's own during remote control when the teacher asks for it.
//!
//! "Low-level" hooks (WH_KEYBOARD_LL, WH_MOUSE_LL): Windows asks our hook about every
//! key press and mouse event on this desktop before any program sees it; we answer
//! "handled" (1), and it goes nowhere. Keys like Win, Alt+Tab, Alt+F4 or Ctrl+Esc are
//! swallowed too; the mouse pointer doesn't even move. The hook runs in the session
//! helper, which is SYSTEM, so it also sees input meant for elevated programs.
//!
//! What it can't stop, by Windows' design: Ctrl+Alt+Del. It opens Windows' own secure
//! screen, where a student can lock the PC, sign out, switch user or shut down. That's
//! fine for a classroom: everything there ends the student's session or leaves the
//! lock as it was (Task Manager opens behind the lock screen, and the hook keeps the
//! mouse away from it). See lock_window.rs for the whole picture.
//!
//! Two details that matter:
//!   * The hook procedure must answer fast: Windows skips a hook that takes too long
//!     and, since Windows 7, silently removes it. So the hooks get a thread of their
//!     own that does nothing else (the lock window's painting can't delay them), and
//!     they are installed again every few seconds in case Windows removed them anyway.
//!   * A hook belongs to the thread that installed it and is called through that
//!     thread's message loop; the thread ending removes it. Dropping `InputBlock`
//!     ends the thread: keyboard and mouse work again.
//!
//! Letting the teacher through (Mode::OnlyTeacher, remote control): Windows marks
//! input that a program made up (SendInput) as "injected", but any program can do
//! that, also a student's. So the helper picks a random number when it starts
//! (`teacher_stamp`) and puts it on every event it injects for the teacher (the
//! events' dwExtraInfo, input_inject.rs); the hooks let through injected events that
//! carry exactly that number. A student can't read the number: it exists only in the
//! memory of this SYSTEM process.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{mpsc, OnceLock};
use std::thread::JoinHandle;

use anyhow::{anyhow, Context, Result};
use windows::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::System::Threading::GetCurrentThreadId;
use windows::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, DispatchMessageW, GetMessageW, KillTimer, PeekMessageW, PostThreadMessageW, SetTimer,
    SetWindowsHookExW, UnhookWindowsHookEx, HHOOK, HOOKPROC, KBDLLHOOKSTRUCT, LLKHF_INJECTED, LLMHF_INJECTED, MSG,
    MSLLHOOKSTRUCT, PM_NOREMOVE, WH_KEYBOARD_LL, WH_MOUSE_LL, WINDOWS_HOOK_ID, WM_QUIT, WM_TIMER, WM_USER,
};

/// How often the hooks are installed again (milliseconds).
const REINSTALL_EVERY_MS: u32 = 5000;

/// What gets through while an `InputBlock` exists.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// Nothing (the screen lock).
    All,
    /// Only the teacher's remote control input (see the top of this file).
    OnlyTeacher,
}

/// The stamp injected events must carry to pass in Mode::OnlyTeacher; 0 = none passes.
/// A static because the hook procedures are plain functions without any context.
static LET_THROUGH: AtomicUsize = AtomicUsize::new(0);

/// This process's random stamp for the teacher's input (never 0), made once.
pub fn teacher_stamp() -> usize {
    static STAMP: OnceLock<usize> = OnceLock::new();
    *STAMP.get_or_init(|| {
        use rand_core::{OsRng, RngCore};
        (OsRng.next_u64() as usize).max(1)
    })
}

/// Switch what gets through (takes effect with the next key or mouse event).
pub fn set_mode(mode: Mode) {
    let stamp = match mode {
        Mode::All => 0,
        Mode::OnlyTeacher => teacher_stamp(),
    };
    LET_THROUGH.store(stamp, Ordering::SeqCst);
}

/// Keyboard and mouse are blocked while this exists.
pub struct InputBlock {
    thread_id: u32,
    thread: Option<JoinHandle<()>>,
}

impl InputBlock {
    pub fn start() -> Result<Self> {
        let (started, result) = mpsc::channel();
        let thread = std::thread::Builder::new().name("input-block".into()).spawn(move || {
            let mut hooks = match Hooks::install() {
                Ok(h) => h,
                Err(e) => {
                    let _ = started.send(Err(e));
                    return;
                }
            };
            // SAFETY: plain calls; PeekMessage makes sure this thread has a message
            // queue before anyone posts WM_QUIT to it.
            let thread_id = unsafe {
                let mut message = MSG::default();
                let _ = PeekMessageW(&mut message, None, WM_USER, WM_USER, PM_NOREMOVE);
                GetCurrentThreadId()
            };
            let _ = started.send(Ok(thread_id));
            run(&mut hooks);
        })?;
        let thread_id = result.recv().map_err(|_| anyhow!("the input-block thread ended"))??;
        log::info!("keyboard and mouse blocked");
        Ok(Self { thread_id, thread: Some(thread) })
    }
}

impl Drop for InputBlock {
    fn drop(&mut self) {
        // SAFETY: posting to the thread we started; WM_QUIT ends its message loop.
        let _ = unsafe { PostThreadMessageW(self.thread_id, WM_QUIT, WPARAM(0), LPARAM(0)) };
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
        log::info!("keyboard and mouse work again");
    }
}

/// The message loop the hooks need, plus the timer that re-installs them.
fn run(hooks: &mut Hooks) {
    // SAFETY: a thread timer (no window): WM_TIMER arrives in this thread's loop.
    let timer = unsafe { SetTimer(None, 0, REINSTALL_EVERY_MS, None) };
    let mut message = MSG::default();
    // SAFETY: the standard message loop; it ends on WM_QUIT (InputBlock's drop).
    unsafe {
        while GetMessageW(&mut message, None, 0, 0).as_bool() {
            if message.message == WM_TIMER {
                hooks.reinstall();
            }
            DispatchMessageW(&message);
        }
        let _ = KillTimer(None, timer);
    }
    // `hooks` is dropped by our caller right after: unhooked.
}

/// The two installed hooks. Dropping removes them.
struct Hooks {
    keyboard: HHOOK,
    mouse: HHOOK,
}

impl Hooks {
    fn install() -> Result<Self> {
        let keyboard = install(WH_KEYBOARD_LL).context("blocking the keyboard")?;
        match install(WH_MOUSE_LL) {
            Ok(mouse) => Ok(Self { keyboard, mouse }),
            Err(e) => {
                // SAFETY: the hook we installed just above.
                let _ = unsafe { UnhookWindowsHookEx(keyboard) };
                Err(e).context("blocking the mouse")
            }
        }
    }

    /// New hooks first, then the old ones off: no moment without a block. (If
    /// Windows had removed one, unhooking it again just fails, harmlessly.)
    fn reinstall(&mut self) {
        for (id, hook) in [(WH_KEYBOARD_LL, &mut self.keyboard), (WH_MOUSE_LL, &mut self.mouse)] {
            match install(id) {
                Ok(new) => {
                    // SAFETY: a hook this thread installed.
                    let _ = unsafe { UnhookWindowsHookEx(*hook) };
                    *hook = new;
                }
                Err(e) => log::warn!("couldn't renew an input hook: {e:#}"),
            }
        }
    }
}

impl Drop for Hooks {
    fn drop(&mut self) {
        // SAFETY: hooks this thread installed.
        unsafe {
            let _ = UnhookWindowsHookEx(self.keyboard);
            let _ = UnhookWindowsHookEx(self.mouse);
        }
    }
}

fn install(id: WINDOWS_HOOK_ID) -> Result<HHOOK> {
    let procedure: HOOKPROC = if id == WH_KEYBOARD_LL { Some(keyboard_hook) } else { Some(mouse_hook) };
    // SAFETY: the hook procedures are 'static functions with the hook signature;
    // low-level hooks need our module's handle but no DLL (they're called in this
    // thread, not injected into others).
    unsafe {
        let module = GetModuleHandleW(None)?;
        Ok(SetWindowsHookExW(id, procedure, Some(module.into()), 0)?)
    }
}

/// Does an event with these properties get through? Only the teacher's: injected, and
/// carrying this process's stamp, while that is allowed (LET_THROUGH not 0).
fn lets_through(injected: bool, extra_info: usize) -> bool {
    let stamp = LET_THROUGH.load(Ordering::Relaxed);
    stamp != 0 && injected && extra_info == stamp
}

/// The keyboard hook: every key ends here. `code` < 0 means "not for you, pass it on"
/// (Windows' rule for hooks); anything else is swallowed unless `lets_through`.
unsafe extern "system" fn keyboard_hook(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code >= 0 {
        // SAFETY: for WH_KEYBOARD_LL, lparam points to a KBDLLHOOKSTRUCT during the call.
        let event = unsafe { &*(lparam.0 as *const KBDLLHOOKSTRUCT) };
        if !lets_through(event.flags.contains(LLKHF_INJECTED), event.dwExtraInfo) {
            return LRESULT(1);
        }
    }
    // SAFETY: forwarding exactly what Windows gave us.
    unsafe { CallNextHookEx(None, code, wparam, lparam) }
}

/// The mouse hook, the same for every mouse event (moves, buttons, wheel).
unsafe extern "system" fn mouse_hook(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code >= 0 {
        // SAFETY: for WH_MOUSE_LL, lparam points to an MSLLHOOKSTRUCT during the call.
        let event = unsafe { &*(lparam.0 as *const MSLLHOOKSTRUCT) };
        if !lets_through(event.flags & LLMHF_INJECTED != 0, event.dwExtraInfo) {
            return LRESULT(1);
        }
    }
    // SAFETY: forwarding exactly what Windows gave us.
    unsafe { CallNextHookEx(None, code, wparam, lparam) }
}
