//! What the agent remembers between restarts (one small JSON file).
//!
//! Windows: C:\ProgramData\SMP\agent.json, readable only by SYSTEM and administrators,
//! the key encrypted with DPAPI for this Windows installation (platform/win/security.rs).
//! Elsewhere (development): a 0600 file.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
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
}
