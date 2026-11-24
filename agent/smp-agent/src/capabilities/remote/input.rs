// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! The teacher's mouse and keyboard, as the agent replays them. Plain Rust (tested on
//! Linux); Windows turns these into SendInput calls (platform/win/input_inject.rs).
//!
//! From the server (which already checked the shapes, server/smp/modules/classroom/remote.py):
//!
//!   {"t": "move", "x": 0.5, "y": 0.25}         pointer position, 0..1 across the primary screen
//!   {"t": "button", "b": 0, "down": true}      0 left, 1 middle, 2 right (the browser's numbering)
//!   {"t": "wheel", "dy": 120, "dx": 0}          Windows' wheel units: 120 = one notch, + = up / right
//!   {"t": "key", "code": "KeyA", "down": true} the physical key (the browser's KeyboardEvent.code)
//!
//! Keys travel as *physical* keys, not as characters: "KeyA" is the key where A is on
//! a US keyboard, and the PC's own keyboard layout decides what it types. So a Hungarian
//! PC types "ő", "ű" and "í" from the keys the teacher presses, as if they sat at it.

use std::collections::HashSet;

use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MouseButton {
    Left,
    Right,
    Middle,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputEvent {
    /// The pointer to (x, y) on the primary screen, 0..=65535 each (Windows' scale).
    Move { x: u16, y: u16 },
    Button { button: MouseButton, down: bool },
    /// 120 = one notch; vertical: + = away from the user, horizontal: + = right.
    Wheel { delta: i32, horizontal: bool },
    /// A keyboard scan code (set 1); `extended`: the E0-prefixed keys (arrows, right
    /// Ctrl/Alt, Windows keys, ...).
    Key { scan: u16, extended: bool, down: bool },
}

/// The server's events -> ours. Anything unknown (a key we have no scan code for, a
/// shape we don't know) is skipped.
pub fn from_server(events: &[Value]) -> Vec<InputEvent> {
    let mut out = vec![];
    for e in events {
        let number = |key: &str| e.get(key).and_then(Value::as_f64);
        let down = e.get("down").and_then(Value::as_bool);
        match e.get("t").and_then(Value::as_str) {
            Some("move") => {
                if let (Some(x), Some(y)) = (number("x"), number("y")) {
                    out.push(InputEvent::Move { x: scale(x), y: scale(y) });
                }
            }
            Some("button") => {
                let button = match e.get("b").and_then(Value::as_u64) {
                    Some(0) => MouseButton::Left,
                    Some(1) => MouseButton::Middle,
                    Some(2) => MouseButton::Right,
                    _ => continue,
                };
                if let Some(down) = down {
                    out.push(InputEvent::Button { button, down });
                }
            }
            Some("wheel") => {
                for (key, horizontal) in [("dy", false), ("dx", true)] {
                    let delta = number(key).unwrap_or(0.0).clamp(-2400.0, 2400.0) as i32;
                    if delta != 0 {
                        out.push(InputEvent::Wheel { delta, horizontal });
                    }
                }
            }
            Some("key") => {
                let code = e.get("code").and_then(Value::as_str).unwrap_or("");
                if let (Some((scan, extended)), Some(down)) = (scan_code(code), down) {
                    out.push(InputEvent::Key { scan, extended, down });
                }
            }
            _ => {}
        }
    }
    out
}

/// 0..1 -> 0..=65535 (out-of-range values are pinned to the edge).
fn scale(v: f64) -> u16 {
    (v.clamp(0.0, 1.0) * 65535.0).round() as u16
}

/// What is held down right now, so the end of a session can let go of it. Without
/// this, a teacher whose connection drops while holding Ctrl would leave the PC with
/// Ctrl stuck down.
#[derive(Debug, Default)]
pub struct Pressed {
    keys: HashSet<(u16, bool)>,
    buttons: HashSet<MouseButton>,
}

impl Pressed {
    pub fn track(&mut self, events: &[InputEvent]) {
        for e in events {
            match *e {
                InputEvent::Key { scan, extended, down: true } => {
                    self.keys.insert((scan, extended));
                }
                InputEvent::Key { scan, extended, down: false } => {
                    self.keys.remove(&(scan, extended));
                }
                InputEvent::Button { button, down: true } => {
                    self.buttons.insert(button);
                }
                InputEvent::Button { button, down: false } => {
                    self.buttons.remove(&button);
                }
                _ => {}
            }
        }
    }

    /// "Let go" events for everything still held, and forget it.
    pub fn release_all(&mut self) -> Vec<InputEvent> {
        let keys = self.keys.drain().map(|(scan, extended)| InputEvent::Key { scan, extended, down: false });
        let buttons = self.buttons.drain().map(|button| InputEvent::Button { button, down: false });
        keys.chain(buttons).collect()
    }
}

/// The browser's KeyboardEvent.code -> (scan code, extended). Scan codes are the
/// keyboard's own numbers for its keys ("set 1"); the PC's layout turns them into
/// characters. Keys a classroom doesn't need (media keys, Japanese keys, ...) are left out.
pub fn scan_code(code: &str) -> Option<(u16, bool)> {
    // Letters and digits follow the rows of the keyboard.
    if let Some(letter) = code.strip_prefix("Key").filter(|l| l.len() == 1) {
        const LETTERS: &str = "QWERTYUIOPASDFGHJKLZXCVBNM";
        const FIRST: [u16; 3] = [0x10, 0x1E, 0x2C]; // first key of the Q, A and Z rows
        let index = LETTERS.find(letter)?;
        let (row, column) = match index {
            0..=9 => (0, index),
            10..=18 => (1, index - 10),
            _ => (2, index - 19),
        };
        return Some((FIRST[row] + column as u16, false));
    }
    if let Some(digit) = code.strip_prefix("Digit").and_then(|d| d.parse::<u16>().ok()).filter(|d| *d <= 9) {
        return Some((if digit == 0 { 0x0B } else { 0x01 + digit }, false));
    }
    if let Some(n) = code.strip_prefix('F').and_then(|n| n.parse::<u16>().ok()) {
        return match n {
            1..=10 => Some((0x3A + n, false)),
            11 => Some((0x57, false)),
            12 => Some((0x58, false)),
            _ => None,
        };
    }
    let key = match code {
        "Escape" => (0x01, false),
        "Minus" => (0x0C, false),
        "Equal" => (0x0D, false),
        "Backspace" => (0x0E, false),
        "Tab" => (0x0F, false),
        "BracketLeft" => (0x1A, false),
        "BracketRight" => (0x1B, false),
        "Enter" => (0x1C, false),
        "ControlLeft" => (0x1D, false),
        "Semicolon" => (0x27, false),
        "Quote" => (0x28, false),
        "Backquote" => (0x29, false), // Hungarian: 0
        "ShiftLeft" => (0x2A, false),
        "Backslash" => (0x2B, false),
        "Comma" => (0x33, false),
        "Period" => (0x34, false),
        "Slash" => (0x35, false),
        "ShiftRight" => (0x36, false),
        "NumpadMultiply" => (0x37, false),
        "AltLeft" => (0x38, false),
        "Space" => (0x39, false),
        "CapsLock" => (0x3A, false),
        "NumLock" => (0x45, true), // sent as an extended key, or Windows takes it for Pause
        "ScrollLock" => (0x46, false),
        "Numpad7" => (0x47, false),
        "Numpad8" => (0x48, false),
        "Numpad9" => (0x49, false),
        "NumpadSubtract" => (0x4A, false),
        "Numpad4" => (0x4B, false),
        "Numpad5" => (0x4C, false),
        "Numpad6" => (0x4D, false),
        "NumpadAdd" => (0x4E, false),
        "Numpad1" => (0x4F, false),
        "Numpad2" => (0x50, false),
        "Numpad3" => (0x51, false),
        "Numpad0" => (0x52, false),
        "NumpadDecimal" => (0x53, false),
        "IntlBackslash" => (0x56, false), // Hungarian: í (left of Y)
        "NumpadEnter" => (0x1C, true),
        "ControlRight" => (0x1D, true),
        "NumpadDivide" => (0x35, true),
        "PrintScreen" => (0x37, true),
        "AltRight" => (0x38, true), // AltGr on Hungarian keyboards (@, €, [ ...)
        "Home" => (0x47, true),
        "ArrowUp" => (0x48, true),
        "PageUp" => (0x49, true),
        "ArrowLeft" => (0x4B, true),
        "ArrowRight" => (0x4D, true),
        "End" => (0x4F, true),
        "ArrowDown" => (0x50, true),
        "PageDown" => (0x51, true),
        "Insert" => (0x52, true),
        "Delete" => (0x53, true),
        "MetaLeft" | "OSLeft" => (0x5B, true), // the Windows key ("OS...": older Firefox)
        "MetaRight" | "OSRight" => (0x5C, true),
        "ContextMenu" => (0x5D, true),
        _ => return None,
    };
    Some(key)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn scan_codes() {
        assert_eq!(scan_code("KeyQ"), Some((0x10, false)));
        assert_eq!(scan_code("KeyP"), Some((0x19, false)));
        assert_eq!(scan_code("KeyA"), Some((0x1E, false)));
        assert_eq!(scan_code("KeyL"), Some((0x26, false)));
        assert_eq!(scan_code("KeyZ"), Some((0x2C, false)));
        assert_eq!(scan_code("KeyM"), Some((0x32, false)));
        assert_eq!(scan_code("Digit1"), Some((0x02, false)));
        assert_eq!(scan_code("Digit9"), Some((0x0A, false)));
        assert_eq!(scan_code("Digit0"), Some((0x0B, false)));
        assert_eq!(scan_code("F1"), Some((0x3B, false)));
        assert_eq!(scan_code("F10"), Some((0x44, false)));
        assert_eq!(scan_code("F12"), Some((0x58, false)));
        assert_eq!(scan_code("ArrowLeft"), Some((0x4B, true)));
        assert_eq!(scan_code("MetaLeft"), Some((0x5B, true)));
        for unknown in ["Key", "KeyAA", "Keyä", "Digit10", "F13", "AudioVolumeUp", ""] {
            assert_eq!(scan_code(unknown), None, "{unknown}");
        }
    }

    #[test]
    fn events_from_the_server() {
        let events = from_server(&[
            json!({"t": "move", "x": 0.5, "y": 1.5}),
            json!({"t": "button", "b": 2, "down": true}),
            json!({"t": "button", "b": 9, "down": true}),
            json!({"t": "wheel", "dy": -240, "dx": 120}),
            json!({"t": "key", "code": "AltRight", "down": true}),
            json!({"t": "key", "code": "LaunchMail", "down": true}),
            json!({"t": "teleport"}),
        ]);
        assert_eq!(events, [
            InputEvent::Move { x: 32768, y: 65535 },
            InputEvent::Button { button: MouseButton::Right, down: true },
            InputEvent::Wheel { delta: -240, horizontal: false },
            InputEvent::Wheel { delta: 120, horizontal: true },
            InputEvent::Key { scan: 0x38, extended: true, down: true },
        ]);
    }

    #[test]
    fn letting_go_of_what_is_held() {
        let mut pressed = Pressed::default();
        let ctrl = |down| InputEvent::Key { scan: 0x1D, extended: false, down };
        let a = |down| InputEvent::Key { scan: 0x1E, extended: false, down };
        pressed.track(&[ctrl(true), a(true), a(false), InputEvent::Button { button: MouseButton::Left, down: true }]);
        let mut released = pressed.release_all();
        released.sort_by_key(|e| format!("{e:?}"));
        assert_eq!(released, [InputEvent::Button { button: MouseButton::Left, down: false }, ctrl(false)]);
        assert!(pressed.release_all().is_empty(), "nothing twice");
    }
}
