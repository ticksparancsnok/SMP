// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Non-Windows implementations (development, tests, a future Linux agent).

use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

/// Wrap the secret key for storage. Here: stored as is, in a file only we can read.
pub fn protect(secret: &[u8]) -> Result<(&'static str, Vec<u8>)> {
    Ok(("none", secret.to_vec()))
}

pub fn unprotect(protection: &str, data: &[u8]) -> Result<Vec<u8>> {
    match protection {
        "none" => Ok(data.to_vec()),
        other => bail!("this build can't unwrap a key protected with {other:?}"),
    }
}

pub fn restrict_file(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

pub fn state_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("SMP_AGENT_STATE_DIR") {
        return dir.into();
    }
    let base = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/state")))
        .unwrap_or_else(|| PathBuf::from("."));
    base.join("smp-agent")
}

/// ("Debian GNU/Linux 13 (trixie)", "13") from /etc/os-release.
pub fn os_info() -> (String, String) {
    let text = std::fs::read_to_string("/etc/os-release").unwrap_or_default();
    let get = |key: &str| {
        text.lines()
            .find_map(|l| l.strip_prefix(&format!("{key}=")))
            .map(|v| v.trim_matches('"').to_string())
    };
    (get("PRETTY_NAME").unwrap_or_else(|| std::env::consts::OS.into()), get("VERSION_ID").unwrap_or_default())
}

pub fn console_user() -> Option<String> {
    std::env::var("SMP_AGENT_USER").or_else(|_| std::env::var("USER")).ok().filter(|u| !u.is_empty())
}

/// No real screen capture here. SMP_AGENT_FAKE_SCREEN=1920x1080 gives a generated test
/// picture of that size (development, and demo "PCs" in containers).
pub fn screen_source() -> Box<dyn crate::capabilities::screen::source::ScreenSource> {
    use crate::capabilities::screen::source::{FakeScreen, NoScreen};
    match std::env::var("SMP_AGENT_FAKE_SCREEN").ok().and_then(|s| FakeScreen::parse(&s)) {
        Some(fake) => {
            log::info!("using a generated {}x{} test screen", fake.width, fake.height);
            Box::new(fake)
        }
        None => Box::new(NoScreen),
    }
}
