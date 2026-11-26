// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! What the agent remembers between restarts (one small JSON file).
//!
//! Windows: C:\ProgramData\SMP\agent.json, readable only by SYSTEM and administrators,
//! the key encrypted with DPAPI for this Windows installation (platform/win/security.rs).
//! Elsewhere (development): a 0600 file.

use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use serde::{Deserialize, Serialize};

use crate::core::identity::Identity;
use crate::platform;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct State {
    /// e.g. "https://192.168.1.10" (what the installer was given)
    pub server: String,
    /// The secret key, wrapped by `platform::protect` (see `protection`).
    pub key: String,
    /// "dpapi-machine" (Windows) or "none" (development builds)
    pub protection: String,
    /// SHA-256 (hex) of the server's TLS certificate, pinned at first contact.
    pub server_cert_sha256: Option<String>,
    /// Set once an admin approved the machine.
    pub device_id: Option<String>,
    /// Which building of the school this PC is in, as the installer typed it (an id or
    /// a name as SMP shows it). Only a hint sent with the join request, so that
    /// building's manager sees the PC waiting; the admin still chooses the room.
    /// Optional in the file: states written by older agents don't have it, and an older
    /// agent reading a newer state file ignores it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub building: Option<String>,
}

/// The longest building hint accepted (an id is 36 characters; names are short).
const MAX_BUILDING_CHARS: usize = 200;

/// Clean up what the installer gave: trimmed, empty means "none".
pub fn normalize_building(input: &str) -> Result<Option<String>> {
    let building = input.trim();
    if building.chars().count() > MAX_BUILDING_CHARS {
        bail!("the building is too long (at most {MAX_BUILDING_CHARS} characters)");
    }
    Ok((!building.is_empty()).then(|| building.to_string()))
}

impl State {
    pub fn new(server: &str, identity: &Identity) -> Result<Self> {
        let (protection, wrapped) = platform::protect(&identity.secret_bytes())?;
        Ok(Self {
            server: server.trim_end_matches('/').to_string(),
            key: STANDARD.encode(wrapped),
            protection: protection.to_string(),
            server_cert_sha256: None,
            device_id: None,
            building: None,
        })
    }

    pub fn identity(&self) -> Result<Identity> {
        let wrapped = STANDARD.decode(&self.key).context("state file: key is not base64")?;
        let secret = platform::unprotect(&self.protection, &wrapped)?;
        Identity::from_secret(&secret)
    }

    pub fn load(path: &Path) -> Result<Option<Self>> {
        match std::fs::read(path) {
            Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes).with_context(|| format!("reading {}", path.display()))?)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e).with_context(|| format!("reading {}", path.display())),
        }
    }

    /// Write atomically (temp file + rename): a power cut can't leave half a file.
    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
        }
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self)?)?;
        platform::restrict_file(&tmp)?;
        std::fs::rename(&tmp, path).with_context(|| format!("writing {}", path.display()))?;
        Ok(())
    }
}

pub fn default_path() -> PathBuf {
    platform::state_dir().join("agent.json")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn save_and_load_keep_the_identity() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sub").join("agent.json");
        let id = Identity::generate();
        let mut state = State::new("https://smp.example/", &id).unwrap();
        state.device_id = Some("abc".into());
        state.save(&path).unwrap();

        let loaded = State::load(&path).unwrap().unwrap();
        assert_eq!(loaded.server, "https://smp.example");
        assert_eq!(loaded.device_id.as_deref(), Some("abc"));
        assert_eq!(loaded.identity().unwrap().fingerprint(), id.fingerprint());
        assert!(State::load(&dir.path().join("missing.json")).unwrap().is_none());
    }

    #[test]
    fn building_round_trips_and_is_left_out_when_unset() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent.json");
        let mut state = State::new("https://smp.example", &Identity::generate()).unwrap();
        state.save(&path).unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(!text.contains("building"), "an unset building must not be written: {text}");
        assert_eq!(State::load(&path).unwrap().unwrap().building, None);

        state.building = Some("Main building".into());
        state.save(&path).unwrap();
        assert_eq!(State::load(&path).unwrap().unwrap().building.as_deref(), Some("Main building"));
    }

    #[test]
    fn a_state_file_from_an_older_agent_loads_without_building() {
        let old = r#"{"server":"https://smp.example","key":"AAAA","protection":"none",
            "server_cert_sha256":null,"device_id":"abc"}"#;
        let state: State = serde_json::from_str(old).unwrap();
        assert_eq!(state.building, None);
        assert_eq!(state.device_id.as_deref(), Some("abc"));
    }

    #[test]
    fn building_is_trimmed_and_capped() {
        assert_eq!(normalize_building("  A wing \n").unwrap().as_deref(), Some("A wing"));
        assert_eq!(normalize_building("   ").unwrap(), None);
        assert!(normalize_building(&"x".repeat(200)).unwrap().is_some());
        assert!(normalize_building(&"x".repeat(201)).is_err());
        assert!(normalize_building(&"é".repeat(200)).unwrap().is_some()); // characters, not bytes
    }
}
