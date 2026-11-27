// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Remote control: replaying the teacher's mouse and keyboard with SendInput, in the
//! session helper (it runs on the user's desktop, as SYSTEM, so its input also reaches
//! elevated windows).
//!
//! * Pointer positions are "absolute": 0..65535 across the *primary* screen, Windows'
//!   own scale, which matches the picture (only the primary screen is captured). The
//!   helper is DPI-aware, so no scaling gets in between.
//! * Keys are sent as scan codes (KEYEVENTF_SCANCODE): the physical key, typed through
//!   the PC's own keyboard layout (see capabilities/remote/input.rs).
//! * Windows' secure desktop (UAC prompts, Ctrl+Alt+Del) doesn't take input from the
//!   user's desktop: there SendInput simply has no effect.
//! * Every event carries this helper's secret stamp (dwExtraInfo), so that while the
//!   teacher blocks the student's input, the hooks still let these through
//!   (input_block.rs).

use windows::Win32::UI::Input::KeyboardAndMouse::{
    SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE, KEYBDINPUT, KEYBD_EVENT_FLAGS, KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_HWHEEL, MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, MOUSEEVENTF_MOVE, MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_WHEEL, MOUSEINPUT, MOUSE_EVENT_FLAGS, VIRTUAL_KEY,
};

use super::input_block::teacher_stamp;
use crate::capabilities::remote::input::{InputEvent, MouseButton};

/// Replay `events` in order (one SendInput call: Windows keeps them together).
pub fn send(events: &[InputEvent]) {
    let inputs: Vec<INPUT> = events.iter().map(to_input).collect();
    if inputs.is_empty() {
        return;
    }
    // SAFETY: a slice of fully initialised INPUT structures and their size.
    let sent = unsafe { SendInput(&inputs, std::mem::size_of::<INPUT>() as i32) };
    if sent as usize != inputs.len() {
        // Mostly: the secure desktop (UAC prompt) is in front. Not worth more than debug.
        log::debug!("SendInput took {sent} of {} events", inputs.len());
    }
}

fn to_input(event: &InputEvent) -> INPUT {
    match *event {
        InputEvent::Move { x, y } => mouse(x as i32, y as i32, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
        InputEvent::Button { button, down } => {
            let flags = match (button, down) {
                (MouseButton::Left, true) => MOUSEEVENTF_LEFTDOWN,
                (MouseButton::Left, false) => MOUSEEVENTF_LEFTUP,
                (MouseButton::Right, true) => MOUSEEVENTF_RIGHTDOWN,
                (MouseButton::Right, false) => MOUSEEVENTF_RIGHTUP,
                (MouseButton::Middle, true) => MOUSEEVENTF_MIDDLEDOWN,
                (MouseButton::Middle, false) => MOUSEEVENTF_MIDDLEUP,
            };
            mouse(0, 0, 0, flags)
        }
        // mouseData is a DWORD, but holds a signed amount for the wheel: the bits of
        // the i32 are passed as they are (`as u32` doesn't change them).
        InputEvent::Wheel { delta, horizontal } => {
            mouse(0, 0, delta as u32, if horizontal { MOUSEEVENTF_HWHEEL } else { MOUSEEVENTF_WHEEL })
        }
        InputEvent::Key { scan, extended, down } => {
            let mut flags = KEYEVENTF_SCANCODE;
            if extended {
                flags |= KEYEVENTF_EXTENDEDKEY;
            }
            if !down {
                flags |= KEYEVENTF_KEYUP;
            }
            keyboard(scan, flags)
        }
    }
}

fn mouse(dx: i32, dy: i32, data: u32, flags: MOUSE_EVENT_FLAGS) -> INPUT {
    INPUT {
        r#type: INPUT_MOUSE,
        Anonymous: INPUT_0 { mi: MOUSEINPUT { dx, dy, mouseData: data, dwFlags: flags, time: 0, dwExtraInfo: teacher_stamp() } },
    }
}

fn keyboard(scan: u16, flags: KEYBD_EVENT_FLAGS) -> INPUT {
    INPUT {
        r#type: INPUT_KEYBOARD,
        // With KEYEVENTF_SCANCODE Windows ignores the virtual key: the scan code decides.
        Anonymous: INPUT_0 { ki: KEYBDINPUT { wVk: VIRTUAL_KEY(0), wScan: scan, dwFlags: flags, time: 0, dwExtraInfo: teacher_stamp() } },
    }
}
