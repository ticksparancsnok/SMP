// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! The SMP tray icon: the always-visible notice that this computer can be monitored
//! (GDPR transparency).
//!
//!   grey     "SMP agent running": nobody is viewing the screen
//!   colour   "monitoring active": a teacher is viewing the screen right now
//!
//! Hovering shows that text, in Hungarian or English (the Windows display language).
//!
//! A tray icon belongs to a window, and a window lives on one thread with a message
//! loop. So the icon gets its own thread; other threads change it by posting a
//! message to that (invisible) window.

use std::cell::RefCell;
use std::sync::mpsc;

use anyhow::{anyhow, Context, Result};
use windows::core::w;
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Shell::{
    Shell_NotifyIconW, NIF_ICON, NIF_TIP, NIM_ADD, NIM_DELETE, NIM_MODIFY, NOTIFYICONDATAW, NOTIFY_ICON_MESSAGE,
};
use windows::Win32::UI::WindowsAndMessaging::{
    ChangeWindowMessageFilterEx, CreateIconFromResourceEx, CreateWindowExW, DefWindowProcW, DestroyWindow,
    DispatchMessageW, GetMessageW, GetSystemMetrics, PostMessageW, PostQuitMessage, RegisterClassW,
    RegisterWindowMessageW, SendMessageW, TranslateMessage, HICON, LR_DEFAULTCOLOR, MSG, MSGFLT_ALLOW, SM_CXSMICON,
    WINDOW_EX_STYLE, WM_APP, WM_DESTROY, WNDCLASSW, WS_OVERLAPPED,
};

const ICON_OFF: [(i32, &[u8]); 4] = [
    (16, include_bytes!("../../../assets/tray-off-16.png")),
    (20, include_bytes!("../../../assets/tray-off-20.png")),
    (24, include_bytes!("../../../assets/tray-off-24.png")),
    (32, include_bytes!("../../../assets/tray-off-32.png")),
];
const ICON_ON: [(i32, &[u8]); 4] = [
    (16, include_bytes!("../../../assets/tray-on-16.png")),
    (20, include_bytes!("../../../assets/tray-on-20.png")),
    (24, include_bytes!("../../../assets/tray-on-24.png")),
    (32, include_bytes!("../../../assets/tray-on-32.png")),
];

const WM_SET_MONITORING: u32 = WM_APP + 1;
const WM_REMOVE: u32 = WM_APP + 2;
const WM_READD: u32 = WM_APP + 3;

/// Handle to the tray thread's window. Dropping it removes the icon.
pub struct Tray {
    window: isize, // HWND as a number: HWND itself may not cross threads
}

impl Tray {
    pub fn start() -> Result<Self> {
        let (started, result) = mpsc::channel();
        std::thread::Builder::new().name("tray".into()).spawn(move || match create_window() {
            Ok(window) => {
                let _ = started.send(Ok(window.0 as isize));
                message_loop();
            }
            Err(e) => {
                let _ = started.send(Err(e));
            }
        })?;
        let window = result.recv().map_err(|_| anyhow!("tray thread ended"))??;
        Ok(Self { window })
    }

    pub fn set_monitoring(&self, monitoring: bool) {
        // SAFETY: posting to our own window; ignored if it's already gone.
        let _ = unsafe { PostMessageW(Some(self.hwnd()), WM_SET_MONITORING, WPARAM(monitoring as usize), LPARAM(0)) };
    }

    /// A handle other threads can keep (unlike `Tray`, dropping it does nothing).
    pub fn notifier(&self) -> TrayNotifier {
        TrayNotifier { window: self.window }
    }

    fn hwnd(&self) -> HWND {
        HWND(self.window as *mut _)
    }
}

/// Lets another thread ask the tray thread to show the icon again.
pub struct TrayNotifier {
    window: isize,
}

impl TrayNotifier {
    /// Remove and add the icon, so Explorer applies changed settings at once.
    pub fn refresh(&self) {
        // SAFETY: posting to our own window; ignored if it's already gone.
        let _ = unsafe { PostMessageW(Some(HWND(self.window as *mut _)), WM_READD, WPARAM(0), LPARAM(0)) };
    }
}

impl Drop for Tray {
    fn drop(&mut self) {
        // SendMessage waits until the tray thread removed the icon.
        // SAFETY: our own window.
        unsafe { SendMessageW(self.hwnd(), WM_REMOVE, None, None) };
    }
}

/// Everything the window procedure needs (it runs on the tray thread only).
struct State {
    monitoring: bool,
    icon_off: HICON,
    icon_on: HICON,
    /// Explorer broadcasts this after it (re)starts: then the icon must be added again.
    taskbar_created: u32,
    hungarian: bool,
}

thread_local! {
    static STATE: RefCell<Option<State>> = const { RefCell::new(None) };
}

fn create_window() -> Result<HWND> {
    // SAFETY: standard window creation; the class name and procedure are 'static.
    unsafe {
        let instance = GetModuleHandleW(None)?;
        let class = WNDCLASSW {
            lpfnWndProc: Some(window_proc),
            hInstance: instance.into(),
            lpszClassName: w!("SMPAgentTray"),
            ..Default::default()
        };
        RegisterClassW(&class);
        // A normal top-level window that's never shown (a "message-only" window
        // wouldn't receive Explorer's TaskbarCreated broadcast).
        let window = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            w!("SMPAgentTray"),
            w!("SMP Agent"),
            WS_OVERLAPPED,
            0,
            0,
            0,
            0,
            None,
            None,
            Some(instance.into()),
            None,
        )
        .context("creating the tray window")?;

        let taskbar_created = RegisterWindowMessageW(w!("TaskbarCreated"));
        // We run as SYSTEM; let Explorer (the user's, lower privilege) reach us.
        let _ = ChangeWindowMessageFilterEx(window, taskbar_created, MSGFLT_ALLOW, None);

        let hungarian = super::system_language_is_hungarian();
        STATE.with(|s| {
            *s.borrow_mut() = Some(State {
                monitoring: false,
                icon_off: load_icon(&ICON_OFF),
                icon_on: load_icon(&ICON_ON),
                taskbar_created,
                hungarian,
            })
        });
        show(window, NIM_ADD); // may fail if Explorer isn't up yet: TaskbarCreated follows
        Ok(window)
    }
}

/// The PNG closest to the size Windows uses for tray icons at this display scaling.
fn load_icon(set: &[(i32, &[u8])]) -> HICON {
    // SAFETY: plain query.
    let want = unsafe { GetSystemMetrics(SM_CXSMICON) };
    let (size, png) = set.iter().find(|(s, _)| *s >= want).unwrap_or(&set[set.len() - 1]);
    // SAFETY: a PNG image in memory (Windows Vista+ accepts PNG icon data).
    unsafe { CreateIconFromResourceEx(png, true, 0x0003_0000, *size, *size, LR_DEFAULTCOLOR) }.unwrap_or_default()
}

fn tooltip(monitoring: bool, hungarian: bool) -> &'static str {
    match (monitoring, hungarian) {
        (false, true) => "SMP ügynök fut – a képernyőt most senki sem nézi",
        (true, true) => "SMP: monitorozás aktív – a tanár látja a képernyőt",
        (false, false) => "SMP agent running – nobody is viewing this screen",
        (true, false) => "SMP: monitoring active – a teacher can see this screen",
    }
}

fn show(window: HWND, action: NOTIFY_ICON_MESSAGE) -> bool {
    STATE.with(|s| {
        let s = s.borrow();
        let Some(state) = s.as_ref() else { return false };
        let mut data = NOTIFYICONDATAW {
            cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
            hWnd: window,
            uID: 1,
            uFlags: NIF_ICON | NIF_TIP,
            hIcon: if state.monitoring { state.icon_on } else { state.icon_off },
            ..Default::default()
        };
        let text: Vec<u16> = tooltip(state.monitoring, state.hungarian).encode_utf16().collect();
        let n = text.len().min(data.szTip.len() - 1);
        data.szTip[..n].copy_from_slice(&text[..n]);
        // SAFETY: data is fully initialised and outlives the call.
        unsafe { Shell_NotifyIconW(action, &data) }.as_bool()
    })
}

extern "system" fn window_proc(window: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    let taskbar_created = STATE.with(|s| s.borrow().as_ref().map(|s| s.taskbar_created).unwrap_or(0));
    match message {
        WM_SET_MONITORING => {
            STATE.with(|s| {
                if let Some(state) = s.borrow_mut().as_mut() {
                    state.monitoring = wparam.0 != 0;
                }
            });
            if !show(window, NIM_MODIFY) {
                show(window, NIM_ADD);
            }
            LRESULT(0)
        }
        WM_READD => {
            show(window, NIM_DELETE);
            show(window, NIM_ADD);
            LRESULT(0)
        }
        WM_REMOVE => {
            show(window, NIM_DELETE);
            // SAFETY: our own window, on its own thread.
            let _ = unsafe { DestroyWindow(window) };
            LRESULT(0)
        }
        WM_DESTROY => {
            // SAFETY: ends this thread's message loop.
            unsafe { PostQuitMessage(0) };
            LRESULT(0)
        }
        m if m != 0 && m == taskbar_created => {
            show(window, NIM_ADD);
            LRESULT(0)
        }
        // SAFETY: default handling for everything else.
        _ => unsafe { DefWindowProcW(window, message, wparam, lparam) },
    }
}

fn message_loop() {
    let mut message = MSG::default();
    // SAFETY: the standard Windows message loop.
    unsafe {
        while GetMessageW(&mut message, None, 0, 0).as_bool() {
            let _ = TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
}
