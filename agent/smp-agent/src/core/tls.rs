//! Which server certificates the agent accepts.
//!
//! SMP's nginx starts with a self-signed certificate, so the usual "is it signed by
//! a trusted CA" check can't be the only rule. The agent accepts a certificate if:
//!   1. the operating system trusts it for this name (a real certificate), or
//!   2. it is exactly the certificate pinned earlier (SHA-256 of its DER bytes), or
//!   3. nothing is pinned yet and this is the first contact (trust on first use).
//!
//! The certificate seen on each connection is reported back (`last_seen`) so the
//! caller can pin it once the server answered, and update the pin when the school
//! later installs a real certificate (rule 1). Anything else is refused: an
//! impostor server can't take over an installed agent.
//!
//! TLS handshake signatures are always verified, also for pinned certificates.

use std::sync::{Arc, Mutex};

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::client::WebPkiServerVerifier;
use rustls::crypto::{verify_tls12_signature, verify_tls13_signature, CryptoProvider};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{DigitallySignedStruct, Error as TlsError, RootCertStore, SignatureScheme};
use sha2::{Digest, Sha256};

use crate::core::identity::hex;

#[derive(Debug)]
pub struct PinningVerifier {
    pinned: Mutex<Option<String>>,
    last_seen: Mutex<Option<String>>,
    system: Option<Arc<WebPkiServerVerifier>>,
    provider: Arc<CryptoProvider>,
}

impl PinningVerifier {
    pub fn new(pinned: Option<String>) -> Arc<Self> {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let mut roots = RootCertStore::empty();
        for cert in rustls_native_certs::load_native_certs().certs {
            let _ = roots.add(cert); // skip certificates rustls can't parse
        }
        let system = if roots.is_empty() {
            None
        } else {
            WebPkiServerVerifier::builder_with_provider(Arc::new(roots), provider.clone()).build().ok()
        };
        Arc::new(Self { pinned: Mutex::new(pinned), last_seen: Mutex::new(None), system, provider })
    }

    /// SHA-256 (hex) of the certificate the last connection presented.
    pub fn last_seen(&self) -> Option<String> {
        self.last_seen.lock().unwrap().clone()
    }

    pub fn pin(&self, fingerprint: Option<String>) {
        *self.pinned.lock().unwrap() = fingerprint;
    }
}

impl ServerCertVerifier for PinningVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, TlsError> {
        let seen = hex(&Sha256::digest(end_entity.as_ref()));
        *self.last_seen.lock().unwrap() = Some(seen.clone());

        if let Some(system) = &self.system {
            if system.verify_server_cert(end_entity, intermediates, server_name, ocsp_response, now).is_ok() {
                return Ok(ServerCertVerified::assertion()); // rule 1: a trusted certificate
            }
        }
        match self.pinned.lock().unwrap().as_deref() {
            Some(pin) if pin == seen => Ok(ServerCertVerified::assertion()), // rule 2
            None => Ok(ServerCertVerified::assertion()),                     // rule 3: first contact
            Some(_) => Err(TlsError::General(
                "the server's certificate changed and isn't trusted by this computer (possible impostor server)".into(),
            )),
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        verify_tls12_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        verify_tls13_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider.signature_verification_algorithms.supported_schemes()
    }
}

/// A rustls client config that uses `verifier`.
pub fn client_config(verifier: Arc<PinningVerifier>) -> Arc<rustls::ClientConfig> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .expect("ring supports TLS 1.2 and 1.3")
        .dangerous() // "dangerous" only because we bring our own verifier (above)
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth();
    Arc::new(config)
}
