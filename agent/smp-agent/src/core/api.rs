// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Talking to the SMP server: signed requests over HTTPS.
//!
//! One `Client` is shared by the main loop and the capabilities' threads (it's
//! wrapped in an `Arc`): ureq's connection pool and our key are both thread-safe.

use std::fmt;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};

use crate::core::identity::Identity;
use crate::core::sysinfo::SysInfo;
use crate::core::tls::{client_config, PinningVerifier};

pub const AGENT_VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Debug)]
pub enum ApiError {
    /// The server doesn't accept this device (not approved, reset, or a bad signature).
    Unauthorized,
    /// The server answered with an error; `code` is SMP's error code (e.g. "classroom.not_watched").
    Http { status: u16, code: Option<String>, message: String },
    /// Couldn't reach the server (network down, DNS, TLS, timeout, ...).
    Transport(String),
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ApiError::Unauthorized => write!(f, "the server doesn't accept this device"),
            ApiError::Http { status, message, .. } => write!(f, "server error {status}: {message}"),
            ApiError::Transport(e) => write!(f, "can't reach the server: {e}"),
        }
    }
}

impl std::error::Error for ApiError {}

impl ApiError {
    /// SMP's error code, if the server sent one.
    pub fn code(&self) -> Option<&str> {
        match self {
            ApiError::Http { code, .. } => code.as_deref(),
            _ => None,
        }
    }
}

/// A screen picture to upload (see capabilities/screen).
pub struct FrameUpload<'a> {
    /// "thumb" or "detail"
    pub kind: &'a str,
    /// The JPEG, or None: "same picture as last time" (a few bytes instead of the JPEG).
    pub jpeg: Option<&'a [u8]>,
    pub width: u32,
    pub height: u32,
    pub screen_width: u32,
    pub screen_height: u32,
}

#[derive(Debug, Serialize)]
struct JoinIn<'a> {
    public_key: String,
    hostname: &'a str,
    mac: Option<&'a str>,
    os_name: &'a str,
    os_version: &'a str,
    agent_version: &'a str,
}

#[derive(Debug, Deserialize)]
pub struct JoinOut {
    /// "pending" | "approved" | "unknown" (rejected/expired: join again)
    pub status: String,
    pub fingerprint_display: String,
}

#[derive(Debug, Serialize)]
pub struct HeartbeatIn {
    pub hostname: String,
    pub agent_version: String,
    pub os_name: String,
    pub os_version: String,
    /// Who is logged in at the console (None: nobody)
    pub user: Option<String>,
    /// The config version we already have; with `wait` the server holds the answer until it changes.
    pub config_version: Option<String>,
    pub wait: bool,
}

#[derive(Debug, Deserialize)]
pub struct HeartbeatOut {
    pub device_id: String,
    pub config: serde_json::Value,
    pub config_version: String,
}

pub struct Client {
    /// ureq keeps finished connections open for reuse. After a network change they're
    /// dead (bound to the old address), so `reset_connections` swaps in a fresh pool.
    http: RwLock<ureq::Agent>,
    server: String,
    identity: Identity,
    verifier: Arc<PinningVerifier>,
    /// server clock minus our clock, seconds (signatures must be within 5 minutes)
    clock_offset: AtomicI64,
}

impl Client {
    pub fn new(server: &str, identity: Identity, pinned_cert: Option<String>) -> Self {
        let verifier = PinningVerifier::new(pinned_cert);
        Self {
            http: RwLock::new(new_http(&verifier)),
            server: server.trim_end_matches('/').to_string(),
            identity,
            verifier,
            clock_offset: AtomicI64::new(0),
        }
    }

    /// Forget pooled connections (call after the network address changed). Requests
    /// already running on an old connection still finish or time out on their own.
    pub fn reset_connections(&self) {
        *self.http.write().unwrap() = new_http(&self.verifier);
    }

    pub fn identity(&self) -> &Identity {
        &self.identity
    }

    /// SHA-256 of the certificate the server presented last (to pin it).
    pub fn certificate_seen(&self) -> Option<String> {
        self.verifier.last_seen()
    }

    pub fn pin_certificate(&self, fingerprint: Option<String>) {
        self.verifier.pin(fingerprint);
    }

    pub fn join(&self, info: &SysInfo) -> Result<JoinOut, ApiError> {
        let body = JoinIn {
            public_key: self.identity.public_key_b64(),
            hostname: &info.hostname,
            mac: info.mac.as_deref(),
            os_name: &info.os_name,
            os_version: &info.os_version,
            agent_version: AGENT_VERSION,
        };
        self.post("/api/v1/agent/join", &body, Duration::from_secs(30))
    }

    pub fn join_status(&self) -> Result<JoinOut, ApiError> {
        self.post("/api/v1/agent/join/status", &serde_json::json!({}), Duration::from_secs(30))
    }

    /// With `wait`, the server may hold the answer up to its long-poll time (~25 s).
    /// The timeout is a little longer than that: a connection that died meanwhile
    /// (network change) is given up after 40 s, not hung on.
    pub fn heartbeat(&self, body: &HeartbeatIn) -> Result<HeartbeatOut, ApiError> {
        self.post("/api/v1/agent/heartbeat", body, Duration::from_secs(if body.wait { 40 } else { 30 }))
    }

    /// Upload a screen picture to `path` (the server tells us where, in the config).
    pub fn put_frame(&self, path: &str, frame: &FrameUpload) -> Result<(), ApiError> {
        let mut headers = vec![
            ("x-smp-frame-kind", frame.kind.to_string()),
            ("x-smp-frame-size", format!("{}x{}", frame.width, frame.height)),
            ("x-smp-screen-size", format!("{}x{}", frame.screen_width, frame.screen_height)),
        ];
        if frame.jpeg.is_none() {
            headers.push(("x-smp-frame-unchanged", "1".to_string()));
        }
        let body = frame.jpeg.unwrap_or(&[]);
        self.send("PUT", path, "image/jpeg", &headers, body, Duration::from_secs(30))?;
        Ok(())
    }

    fn post<B: Serialize, R: DeserializeOwned>(&self, path: &str, body: &B, timeout: Duration) -> Result<R, ApiError> {
        let raw = serde_json::to_vec(body).map_err(|e| ApiError::Transport(e.to_string()))?;
        let resp = self.send("POST", path, "application/json", &[], &raw, timeout)?;
        resp.into_json::<R>().map_err(|e| ApiError::Transport(format!("bad response: {e}")))
    }

    /// Send one signed request (see identity.rs for what is signed).
    fn send(
        &self,
        method: &str,
        path: &str,
        content_type: &str,
        headers: &[(&str, String)],
        body: &[u8],
        timeout: Duration,
    ) -> Result<ureq::Response, ApiError> {
        for attempt in 0..2 {
            let offset_used = self.clock_offset.load(Ordering::Relaxed);
            let ts = unix_now() + offset_used;
            let http = self.http.read().unwrap().clone(); // a cheap handle to the pool
            let mut request = http
                .request(method, &format!("{}{}", self.server, path))
                .timeout(timeout)
                .set("content-type", content_type)
                .set("x-smp-key", &self.identity.fingerprint())
                .set("x-smp-time", &ts.to_string())
                .set("x-smp-signature", &self.identity.sign(method, path, ts, body));
            for (name, value) in headers {
                request = request.set(name, value);
            }
            match request.send_bytes(body) {
                Ok(resp) => {
                    self.learn_clock(resp.header("date"));
                    return Ok(resp);
                }
                Err(ureq::Error::Status(401, resp)) => {
                    // Maybe only our clock is wrong: learn the server's time and retry once.
                    self.learn_clock(resp.header("date"));
                    let corrected = (self.clock_offset.load(Ordering::Relaxed) - offset_used).abs() > 60;
                    if attempt == 0 && corrected {
                        log::warn!("this computer's clock is off by {} s; retrying with the server's time",
                                   self.clock_offset.load(Ordering::Relaxed));
                        continue;
                    }
                    return Err(ApiError::Unauthorized);
                }
                Err(ureq::Error::Status(status, resp)) => {
                    // SMP errors look like {"detail": "...", "code": "...", "params": {...}}
                    let text = resp.into_string().unwrap_or_default();
                    let json: Option<serde_json::Value> = serde_json::from_str(&text).ok();
                    let field = |name: &str| json.as_ref().and_then(|j| j.get(name)?.as_str().map(str::to_string));
                    let message = field("detail").unwrap_or_else(|| text.chars().take(300).collect());
                    return Err(ApiError::Http { status, code: field("code"), message });
                }
                Err(ureq::Error::Transport(t)) => return Err(ApiError::Transport(t.to_string())),
            }
        }
        Err(ApiError::Unauthorized)
    }

    fn learn_clock(&self, date_header: Option<&str>) {
        let Some(server) = date_header.and_then(|d| httpdate::parse_http_date(d).ok()) else { return };
        let Ok(server) = server.duration_since(UNIX_EPOCH) else { return };
        self.clock_offset.store(server.as_secs() as i64 - unix_now(), Ordering::Relaxed);
    }
}

fn new_http(verifier: &Arc<PinningVerifier>) -> ureq::Agent {
    ureq::AgentBuilder::new()
        .tls_config(client_config(verifier.clone()))
        .timeout_connect(Duration::from_secs(10))
        .user_agent(&format!("smp-agent/{AGENT_VERSION}"))
        .build()
}

fn unix_now() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}
