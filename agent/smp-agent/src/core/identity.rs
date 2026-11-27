// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! The device's identity: an ECDSA P-256 key pair created at install.
//!
//! The public key (uncompressed SEC1 point, 65 bytes) identifies the device to the
//! server; its SHA-256 is the fingerprint an admin compares before approving the
//! machine. Every request is signed (see `signed_message`), so there's no token
//! that could be copied off the disk and reused elsewhere.
//!
//! Must match server/smp/modules/agent/crypto.py byte for byte.

use anyhow::{Context, Result};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use p256::ecdsa::signature::Signer;
use p256::ecdsa::{Signature, SigningKey};
use sha2::{Digest, Sha256};

pub const PROTOCOL: &str = "SMP-AGENT-1";

pub struct Identity {
    key: SigningKey,
}

impl Identity {
    /// A brand-new random key (from the OS's secure random generator).
    pub fn generate() -> Self {
        Self { key: SigningKey::random(&mut rand_core::OsRng) }
    }

    /// Restore a key from its 32 secret bytes (as returned by `secret_bytes`).
    pub fn from_secret(secret: &[u8]) -> Result<Self> {
        let key = SigningKey::from_slice(secret).context("stored key is not a valid P-256 key")?;
        Ok(Self { key })
    }

    pub fn secret_bytes(&self) -> Vec<u8> {
        self.key.to_bytes().to_vec()
    }

    /// 0x04 || X || Y
    pub fn public_point(&self) -> Vec<u8> {
        self.key.verifying_key().to_encoded_point(false).as_bytes().to_vec()
    }

    pub fn public_key_b64(&self) -> String {
        URL_SAFE_NO_PAD.encode(self.public_point())
    }

    /// SHA-256 of the public point, hex: the device's ID on the server.
    pub fn fingerprint(&self) -> String {
        hex(&Sha256::digest(self.public_point()))
    }

    /// What people compare on screen: the first 80 bits, "3F2A-91C4-0B7E-55D1-AA20".
    pub fn display_fingerprint(&self) -> String {
        display_fingerprint(&self.fingerprint())
    }

    /// The X-SMP-Signature header value for a request.
    pub fn sign(&self, method: &str, path_and_query: &str, unix_time: i64, body: &[u8]) -> String {
        let message = signed_message(method, path_and_query, unix_time, body);
        let signature: Signature = self.key.sign(&message); // SHA-256 + ECDSA P-256
        URL_SAFE_NO_PAD.encode(signature.to_bytes()) // r || s, 64 bytes
    }
}

/// "SMP-AGENT-1\nPOST\n/api/v1/agent/heartbeat\n1732190000\n<hex sha256(body)>"
pub fn signed_message(method: &str, path_and_query: &str, unix_time: i64, body: &[u8]) -> Vec<u8> {
    format!(
        "{PROTOCOL}\n{}\n{path_and_query}\n{unix_time}\n{}",
        method.to_uppercase(),
        hex(&Sha256::digest(body))
    )
    .into_bytes()
}

pub fn display_fingerprint(fingerprint: &str) -> String {
    let head: String = fingerprint.chars().take(20).collect::<String>().to_uppercase();
    head.as_bytes().chunks(4).map(|c| std::str::from_utf8(c).unwrap()).collect::<Vec<_>>().join("-")
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use p256::ecdsa::signature::Verifier;
    use p256::ecdsa::VerifyingKey;

    #[test]
    fn message_format_matches_the_server() {
        // Same construction as crypto.signed_message() in the Python server.
        let m = signed_message("post", "/api/v1/agent/heartbeat", 1_732_190_000, b"{}");
        assert_eq!(
            String::from_utf8(m).unwrap(),
            "SMP-AGENT-1\nPOST\n/api/v1/agent/heartbeat\n1732190000\n\
             44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        );
    }

    #[test]
    fn signatures_verify_and_keys_round_trip() {
        let id = Identity::generate();
        let sig_b64 = id.sign("POST", "/x", 1, b"body");
        let sig = Signature::from_slice(&URL_SAFE_NO_PAD.decode(sig_b64).unwrap()).unwrap();
        let vk = VerifyingKey::from_sec1_bytes(&id.public_point()).unwrap();
        assert!(vk.verify(&signed_message("POST", "/x", 1, b"body"), &sig).is_ok());
        assert!(vk.verify(&signed_message("POST", "/x", 1, b"other"), &sig).is_err());

        let restored = Identity::from_secret(&id.secret_bytes()).unwrap();
        assert_eq!(restored.fingerprint(), id.fingerprint());
        assert_eq!(id.public_point().len(), 65);
        assert_eq!(id.fingerprint().len(), 64);
    }

    #[test]
    fn display_fingerprint_groups() {
        assert_eq!(display_fingerprint("3f2a91c40b7e55d1aa20ffff"), "3F2A-91C4-0B7E-55D1-AA20");
    }
}
