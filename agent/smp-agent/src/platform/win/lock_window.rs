// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! The lock screen: a window over every monitor with the school's logo and the
//! teacher's message, while keyboard and mouse are blocked (input_block.rs).
//!
//! It runs in the session helper (helper.rs), which runs as SYSTEM on the user's
//! desktop: the student can't close or kill it (Task Manager can't end SYSTEM
//! processes for a normal user), and a new helper after logoff/logon gets the lock
//! again from the service (session.rs).
//!
//! How it stays in front:
//!   * one borderless "topmost" window covering the whole virtual screen (all
//!     monitors, wherever they are), no taskbar button;
//!   * every half second it puts itself on top again (another topmost window, e.g.
//!     Task Manager set to "always on top", could have come up) and follows changes of
//!     the monitor layout;
//!   * the mouse pointer is hidden over it.
//!
//! What the lock does NOT stop (Windows doesn't let any program): Ctrl+Alt+Del, and the
//! secure screen it opens (lock, sign out, switch user, shut down, Task Manager). Signing
//! out or shutting down ends the session; everything else comes back to the lock. Also
//! the power button, of course. The lock is a classroom tool ("eyes to the front"), not
//! a security boundary; it goes away whenever the agent stops (fail-open, see
//! capabilities/lock/mod.rs).
//!
//! Like the tray icon, the window lives on its own thread with a message loop; other
//! threads talk to it by posting messages.

use std::cell::RefCell;
use std::sync::{mpsc, Arc, Mutex};
use std::thread::JoinHandle;

use anyhow::{anyhow, Context, Result};
use windows::core::{w, BOOL};
use windows::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::Graphics::Gdi::{
    BeginPaint, CreateFontW, CreateSolidBrush, DeleteObject, DrawTextW, EndPaint, EnumDisplayMonitors, FillRect,
    InvalidateRect, SelectObject, SetBkMode, SetTextColor, StretchDIBits, BITMAPINFO, BITMAPINFOHEADER, BI_RGB,
    CLEARTYPE_QUALITY, CLIP_DEFAULT_PRECIS, DEFAULT_CHARSET, DIB_RGB_COLORS, DRAW_TEXT_FORMAT, DT_CALCRECT, DT_CENTER,
    DT_NOPREFIX, DT_WORDBREAK, FF_SWISS, FW_SEMIBOLD, HDC, HMONITOR, OUT_DEFAULT_PRECIS, PAINTSTRUCT, SRCCOPY,
    TRANSPARENT,
};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetForegroundWindow, GetMessageW,
    GetSystemMetrics, GetWindowRect, PostMessageW, PostQuitMessage, RegisterClassW, SetCursor, SetForegroundWindow,
    SetTimer, SetWindowPos, ShowWindow, TranslateMessage, HWND_TOPMOST, MSG, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN,
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SWP_NOACTIVATE, SWP_SHOWWINDOW, SW_SHOW, WM_APP, WM_CLOSE, WM_DESTROY,
    WM_DISPLAYCHANGE, WM_ERASEBKGND, WM_PAINT, WM_SETCURSOR, WM_TIMER, WNDCLASSW, WS_EX_TOOLWINDOW, WS_EX_TOPMOST,
    WS_POPUP,
};

use super::input_block::InputBlock;
use crate::capabilities::lock::picture::{self, Image, BACKGROUND, TEXT};

const WM_UPDATE: u32 = WM_APP + 1; // the message or logo changed
const WM_UNLOCK: u32 = WM_APP + 2; // close for good
const KEEP_ON_TOP_TIMER: usize = 1;
const KEEP_ON_TOP_MS: u32 = 500;

/// What the lock screen shows.
struct Content {
    text: String,
    png: Vec<u8>,
}

/// A shown lock screen. Dropping it unlocks: the window closes and keyboard and
/// mouse work again.
pub struct LockWindow {
    window: isize, // HWND as a number: HWND itself may not cross threads
    content: Arc<Mutex<Content>>,
    thread: Option<JoinHandle<()>>,
    /// Declared last: dropped after the window is gone (fields drop in order).
    _input: Option<InputBlock>,
}

impl LockWindow {
    /// Show the lock screen with `text` (the capability always gives one) and `png` (the logo).
    pub fn start(text: String, png: Vec<u8>) -> Result<Self> {
        let content = Arc::new(Mutex::new(Content { text, png }));
        let (started, result) = mpsc::channel();
        let for_thread = content.clone();
        let thread = std::thread::Builder::new().name("lock-window".into()).spawn(move || {
            match create_window(for_thread) {
                Ok(window) => {
                    let _ = started.send(Ok(window.0 as isize));
                    message_loop();
                }
                Err(e) => {
                    let _ = started.send(Err(e));
                }
            }
        })?;
        let window = result.recv().map_err(|_| anyhow!("the lock window thread ended"))??;
        // Without the input block the screen is still covered; say so and carry on.
        let input = InputBlock::start().map_err(|e| log::error!("keyboard and mouse stay usable: {e:#}")).ok();
        log::info!("lock screen shown");
        Ok(Self { window, content, thread: Some(thread), _input: input })
    }

    /// Show another message or logo.
    pub fn update(&self, text: String, png: Vec<u8>) {
        *self.content.lock().unwrap() = Content { text, png };
        // SAFETY: posting to our own window; ignored if it's already gone.
        let _ = unsafe { PostMessageW(Some(self.hwnd()), WM_UPDATE, WPARAM(0), LPARAM(0)) };
    }

    fn hwnd(&self) -> HWND {
        HWND(self.window as *mut _)
    }
}

impl Drop for LockWindow {
    fn drop(&mut self) {
        // SAFETY: posting to our own window; its thread destroys it and ends.
        let _ = unsafe { PostMessageW(Some(self.hwnd()), WM_UNLOCK, WPARAM(0), LPARAM(0)) };
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
        log::info!("lock screen closed");
    }
}

/// Everything the window procedure needs (it runs on the window's thread only).
struct State {
    content: Arc<Mutex<Content>>,
    /// The logo prepared for each monitor size seen: (width, height) -> picture.
    /// Emptied when the content changes.
    logos: Vec<((i32, i32), Option<Image>)>,
}

thread_local! {
    static STATE: RefCell<Option<State>> = const { RefCell::new(None) };
}

/// The whole virtual screen (all monitors): x, y, width, height.
fn virtual_screen() -> (i32, i32, i32, i32) {
    // SAFETY: plain queries.
    unsafe {
        (
            GetSystemMetrics(SM_XVIRTUALSCREEN),
            GetSystemMetrics(SM_YVIRTUALSCREEN),
            GetSystemMetrics(SM_CXVIRTUALSCREEN),
            GetSystemMetrics(SM_CYVIRTUALSCREEN),
        )
    }
}

fn create_window(content: Arc<Mutex<Content>>) -> Result<HWND> {
    STATE.with(|s| {
        *s.borrow_mut() = Some(State { content, logos: vec![] })
    });
    // SAFETY: standard window creation; the class name and procedure are 'static.
    unsafe {
        let instance = GetModuleHandleW(None)?;
        let class = WNDCLASSW {
            lpfnWndProc: Some(window_proc),
            hInstance: instance.into(),
            lpszClassName: w!("SMPAgentLock"),
            ..Default::default()
        };
        RegisterClassW(&class); // fails harmlessly if an earlier lock registered it
        let (x, y, width, height) = virtual_screen();
        let window = CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW, // above normal windows; no taskbar button
            w!("SMPAgentLock"),
            w!("SMP"),
            WS_POPUP, // no border, no title bar
            x,
            y,
            width,
            height,
            None,
            None,
            Some(instance.into()),
            None,
        )
        .context("creating the lock window")?;
        let _ = ShowWindow(window, SW_SHOW);
        let _ = SetForegroundWindow(window);
        SetTimer(Some(window), KEEP_ON_TOP_TIMER, KEEP_ON_TOP_MS, None);
        Ok(window)
    }
}

/// On top of every other topmost window, covering all monitors (they may have changed).
fn keep_on_top(window: HWND) {
    let (x, y, width, height) = virtual_screen();
    // SAFETY: our own window.
    unsafe {
        let _ = SetWindowPos(window, Some(HWND_TOPMOST), x, y, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW);
        if GetForegroundWindow() != window {
            let _ = SetForegroundWindow(window); // best effort: Windows may refuse
        }
    }
}

extern "system" fn window_proc(window: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match message {
        WM_PAINT => {
            paint(window);
            LRESULT(0)
        }
        WM_ERASEBKGND => LRESULT(1), // paint() fills everything; no flicker
        WM_SETCURSOR => {
            // SAFETY: hides the pointer over our window.
            unsafe { SetCursor(None) };
            LRESULT(1)
        }
        WM_TIMER => {
            keep_on_top(window);
            LRESULT(0)
        }
        WM_DISPLAYCHANGE => {
            keep_on_top(window); // a monitor came or went: cover the new layout
            invalidate(window);
            LRESULT(0)
        }
        WM_UPDATE => {
            STATE.with(|s| {
                if let Some(state) = s.borrow_mut().as_mut() {
                    state.logos.clear();
                }
            });
            invalidate(window);
            LRESULT(0)
        }
        WM_CLOSE => LRESULT(0), // only the teacher closes it (WM_UNLOCK)
        WM_UNLOCK => {
            // SAFETY: our own window, on its own thread.
            let _ = unsafe { DestroyWindow(window) };
            LRESULT(0)
        }
        WM_DESTROY => {
            // SAFETY: ends this thread's message loop.
            unsafe { PostQuitMessage(0) };
            LRESULT(0)
        }
        // SAFETY: default handling for everything else.
        _ => unsafe { DefWindowProcW(window, message, wparam, lparam) },
    }
}

fn invalidate(window: HWND) {
    // SAFETY: our own window; None = all of it.
    let _ = unsafe { InvalidateRect(Some(window), None, false) };
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

// ---------------------------------------------------------------------- painting

/// COLORREF is 0x00BBGGRR.
fn colorref([r, g, b]: [u8; 3]) -> COLORREF {
    COLORREF(r as u32 | (g as u32) << 8 | (b as u32) << 16)
}

fn paint(window: HWND) {
    let mut ps = PAINTSTRUCT::default();
    // SAFETY: BeginPaint/EndPaint pair on our own window; every GDI object made here
    // is deleted again.
    unsafe {
        let dc = BeginPaint(window, &mut ps);
        let brush = CreateSolidBrush(colorref(BACKGROUND));
        FillRect(dc, &ps.rcPaint, brush);
        let _ = DeleteObject(brush.into());

        // Monitors are in screen coordinates; the window starts at the virtual
        // screen's corner (which can be negative: a monitor left of the main one).
        let mut origin = RECT::default();
        let _ = GetWindowRect(window, &mut origin);
        for m in monitors() {
            let area = RECT { left: m.left - origin.left, top: m.top - origin.top, right: m.right - origin.left, bottom: m.bottom - origin.top };
            paint_monitor(dc, area);
        }
        let _ = EndPaint(window, &ps);
    }
}

/// Every monitor's rectangle (screen coordinates).
fn monitors() -> Vec<RECT> {
    unsafe extern "system" fn add(_: HMONITOR, _: HDC, rect: *mut RECT, list: LPARAM) -> BOOL {
        // SAFETY: `list` is the Vec passed below; `rect` is valid during the call.
        unsafe { (*(list.0 as *mut Vec<RECT>)).push(*rect) };
        true.into()
    }
    let mut list: Vec<RECT> = vec![];
    // SAFETY: the callback only runs during this call, while `list` exists.
    let _ = unsafe { EnumDisplayMonitors(None, None, Some(add), LPARAM(&mut list as *mut Vec<RECT> as isize)) };
    list
}

/// The logo and the message, centred on one monitor (`area`, window coordinates).
fn paint_monitor(dc: HDC, area: RECT) {
    let (width, height) = (area.right - area.left, area.bottom - area.top);
    if width <= 0 || height <= 0 {
        return;
    }
    let (text, logo) = STATE.with(|s| {
        let mut s = s.borrow_mut();
        let state = s.as_mut().expect("set when the window was created");
        let content = state.content.lock().unwrap();
        let text = content.text.clone();
        let logo = match state.logos.iter().find(|(size, _)| *size == (width, height)) {
            Some((_, logo)) => logo.clone(),
            None => {
                let logo = prepare_logo(&content.png, width, height);
                state.logos.push(((width, height), logo.clone()));
                logo
            }
        };
        (text, logo)
    });

    // The message: wide lines, big letters; smaller letters while it's too tall.
    let text_width = width * 7 / 10;
    let mut font_height = (height / 16).max(16);
    let mut wide: Vec<u16> = text.encode_utf16().collect();
    let format = DT_CENTER | DT_WORDBREAK | DT_NOPREFIX;
    let mut text_height;
    loop {
        text_height = draw_text(dc, &mut wide, font_height, RECT { left: 0, top: 0, right: text_width, bottom: 0 }, format | DT_CALCRECT);
        if text_height <= height / 2 || font_height <= 16 {
            break;
        }
        font_height = (font_height * 4 / 5).max(16);
    }

    // Logo above the text, the two together centred vertically.
    let gap = if logo.is_some() { height / 20 } else { 0 };
    let logo_height = logo.as_ref().map(|l| l.height as i32).unwrap_or(0);
    let top = area.top + ((height - logo_height - gap - text_height) / 2).max(0);
    if let Some(logo) = &logo {
        draw_image(dc, logo, area.left + (width - logo.width as i32) / 2, top);
    }
    let left = area.left + (width - text_width) / 2;
    let text_top = top + logo_height + gap;
    draw_text(dc, &mut wide, font_height, RECT { left, top: text_top, right: left + text_width, bottom: text_top + text_height }, format);
}

/// The logo at most 40 % of the monitor's width and 30 % of its height; SMP's when the
/// school's can't be read (logged), nothing if even that fails.
fn prepare_logo(png: &[u8], width: i32, height: i32) -> Option<Image> {
    let (max_w, max_h) = ((width * 2 / 5).max(1) as u32, (height * 3 / 10).max(1) as u32);
    picture::logo_on_background(png, max_w, max_h)
        .or_else(|e| {
            log::warn!("the lock screen logo can't be shown ({e:#}); showing SMP's");
            picture::logo_on_background(picture::SMP_LOGO, max_w, max_h)
        })
        .ok()
}

/// Draw (or with DT_CALCRECT only measure) `text`; returns the height it takes.
fn draw_text(dc: HDC, text: &mut [u16], font_height: i32, mut rect: RECT, format: DRAW_TEXT_FORMAT) -> i32 {
    // SAFETY: the font is selected only for this call and deleted afterwards.
    unsafe {
        let font = CreateFontW(
            -font_height, // negative: the letters' height, not the cell's
            0,
            0,
            0,
            FW_SEMIBOLD.0 as i32,
            0,
            0,
            0,
            DEFAULT_CHARSET,
            OUT_DEFAULT_PRECIS,
            CLIP_DEFAULT_PRECIS,
            CLEARTYPE_QUALITY,
            FF_SWISS.0 as u32,
            w!("Segoe UI"),
        );
        let previous = SelectObject(dc, font.into());
        SetBkMode(dc, TRANSPARENT);
        SetTextColor(dc, colorref(TEXT));
        let height = DrawTextW(dc, text, &mut rect, format);
        SelectObject(dc, previous);
        let _ = DeleteObject(font.into());
        height
    }
}

/// An opaque BGRA picture at (x, y), pixel for pixel.
fn draw_image(dc: HDC, image: &Image, x: i32, y: i32) {
    let (w, h) = (image.width as i32, image.height as i32);
    let info = BITMAPINFO {
        bmiHeader: BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: w,
            biHeight: -h, // negative: top row first
            biPlanes: 1,
            biBitCount: 32,
            biCompression: BI_RGB.0,
            ..Default::default()
        },
        ..Default::default()
    };
    // SAFETY: `image.bgra` holds w * h * 4 bytes, as the header says.
    unsafe {
        StretchDIBits(dc, x, y, w, h, 0, 0, w, h, Some(image.bgra.as_ptr().cast()), &info, DIB_RGB_COLORS, SRCCOPY);
    }
}
