// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Talking to the SMP server: signed requests over HTTPS.
//!
//! One `Client` is shared by the main loop and the capabilities' threads (it's
//! wrapped in an `Arc`): ureq's connection pool and our key are both thread-safe.

use std::fmt;
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::core::identity::{hex, Identity};
use crate::core::presence::PresenceProbe;
use crate::core::sysinfo::SysInfo;
use crate::core::tls::{client_config, PinningVerifier};

/// [agent] version of Version.toml in the repository root, compiled in by build.rs.
pub const AGENT_VERSION: &str = env!("SMP_AGENT_VERSION");

/// Only answers that came back within this tell us the server's time (see `clock_offset`).
const CLOCK_ANSWER_WITHIN: Duration = Duration::from_secs(3);

/// Which build of the agent this is; the server offers updates for it (heartbeat).
#[cfg(all(windows, target_arch = "x86_64"))]
pub const PLATFORM: &str = "windows-x86_64";
#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
pub const PLATFORM: &str = "linux-x86_64";
#[cfg(not(any(all(windows, target_arch = "x86_64"), all(target_os = "linux", target_arch = "x86_64"))))]
pub const PLATFORM: &str = "other";

#[derive(Debug)]
pub enum ApiError {
    /// The server doesn't accept this device (not approved, reset, or a bad signature).
    Unauthorized,
    /// The server answered with an error; `code` is SMP's error code (e.g. "classroom.not_watched").
    /// `retry_after`: seconds, when the server said when to come back (e.g. "software.busy").
    Http { status: u16, code: Option<String>, message: String, retry_after: Option<u64> },
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

/// A screen picture to upload (see capabilities/screen and capabilities/remote).
pub struct FrameUpload<'a> {
    /// "thumb", "detail" or "remote"
    pub kind: &'a str,
    /// Remote control: the session the picture belongs to (the server drops pictures
    /// of a session that has ended).
    pub session: Option<&'a str>,
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
    /// This computer's own IP (see HeartbeatIn::address).
    address: Option<String>,
    /// The building the installer named (State::building): a hint for SMP, so that
    /// building's manager sees the request. Left out when not set (older servers
    /// ignore the field anyway).
    #[serde(skip_serializing_if = "Option::is_none")]
    building: Option<String>,
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
    /// [`PLATFORM`]: which build the server offers as an update.
    pub platform: &'static str,
    /// This computer's own IP: the one it uses to reach the server (core/presence.rs).
    /// The server shows it as the device's address and uses it for "by network" rooms;
    /// it can't see it itself when the computer is behind a NAT.
    pub address: Option<String>,
    /// What this agent can do (its capabilities' names): SMP offers features, such as
    /// remote control, only to computers whose agent has them.
    pub capabilities: Vec<&'static str>,
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
    /// Counts requests made with `unique` (see there).
    request_counter: AtomicU64,
    /// Sent with every join request (State::building); set once the state is loaded,
    /// or when the installer answers the question after the first request.
    building: RwLock<Option<String>>,
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
            request_counter: AtomicU64::new(0),
            building: RwLock::new(None),
        }
    }

    pub fn set_building(&self, building: Option<String>) {
        *self.building.write().unwrap() = building;
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
            address: PresenceProbe::new(&self.server).local_address().map(|a| a.to_string()),
            building: self.building.read().unwrap().clone(),
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
        if let Some(session) = frame.session {
            headers.push(("x-smp-remote-session", session.to_string()));
        }
        let body = frame.jpeg.unwrap_or(&[]);
        self.send("PUT", path, "image/jpeg", &headers, body, Duration::from_secs(30))?;
        Ok(())
    }

    /// Download `path` (a file the server offers, e.g. an agent update) to `dest`, at most
    /// `max_bytes`. Returns its size and SHA-256 (hex); the caller compares them with
    /// what the server announced. Plain GET (the file is public), over the same pinned TLS.
    pub fn download(&self, path: &str, dest: &Path, max_bytes: u64) -> anyhow::Result<(u64, String)> {
        use anyhow::{bail, Context};
        let http = self.http.read().unwrap().clone();
        let resp = match http.get(&format!("{}{}", self.server, path)).timeout(Duration::from_secs(600)).call() {
            Ok(resp) => resp,
            Err(ureq::Error::Status(status, resp)) => bail!("the server answered {status} {} for {path}", resp.status_text()),
            Err(ureq::Error::Transport(t)) => bail!("can't reach the server: {t}"),
        };
        let mut file = std::fs::File::create(dest).with_context(|| format!("creating {}", dest.display()))?;
        let mut reader = resp.into_reader().take(max_bytes + 1);
        let (mut hasher, mut size, mut buf) = (Sha256::new(), 0u64, vec![0u8; 64 * 1024]);
        loop {
            let n = reader.read(&mut buf).context("the download was interrupted")?;
            if n == 0 {
                break;
            }
            size += n as u64;
            if size > max_bytes {
                bail!("the download is larger than the {max_bytes} bytes expected");
            }
            hasher.update(&buf[..n]);
            file.write_all(&buf[..n]).with_context(|| format!("writing {}", dest.display()))?;
        }
        file.sync_all().with_context(|| format!("writing {}", dest.display()))?;
        Ok((size, hex(&hasher.finalize())))
    }

    /// A small file only approved devices may fetch (e.g. the lock screen's logo): a
    /// signed GET, at most `max_bytes`. Unlike `download` it's held in memory.
    pub fn get_bytes(&self, path: &str, max_bytes: u64) -> Result<Vec<u8>, ApiError> {
        let resp = self.send("GET", path, "application/octet-stream", &[], &[], Duration::from_secs(60))?;
        let mut bytes = Vec::new();
        resp.into_reader()
            .take(max_bytes + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| ApiError::Transport(format!("reading {path}: {e}")))?;
        if bytes.len() as u64 > max_bytes {
            return Err(ApiError::Transport(format!("{path} is larger than {max_bytes} bytes")));
        }
        Ok(bytes)
    }

    /// Download a large file only approved devices may fetch (a software installer) to
    /// `dest`, a signed GET. If `dest` already holds the start of it (an earlier attempt
    /// was cut off), only the rest is asked for (Range) and appended. Returns the whole
    /// file's size and SHA-256 (hex); the caller compares them with what the server
    /// announced. At most `max_bytes`.
    ///
    /// No overall time limit that a slow network could hit: a stalled connection fails
    /// after `STALL` without data, and the next attempt continues where this one stopped.
    pub fn download_resumable(&self, path: &str, dest: &Path, max_bytes: u64) -> Result<(u64, String), ApiError> {
        const STALL: Duration = Duration::from_secs(60);
        let io = |what: &str, e: std::io::Error| ApiError::Transport(format!("{what} {}: {e}", dest.display()));
        // What we already have: hash it again (the hash can't be saved halfway).
        let (mut hasher, mut have) = (Sha256::new(), 0u64);
        if let Ok(mut f) = std::fs::File::open(dest) {
            let mut buf = vec![0u8; 256 * 1024];
            loop {
                let n = f.read(&mut buf).map_err(|e| io("reading", e))?;
                if n == 0 {
                    break;
                }
                hasher.update(&buf[..n]);
                have += n as u64;
            }
        }
        if have >= max_bytes {
            // Complete already (the agent stopped between downloading and using it):
            // nothing to ask for; the caller checks the hash as always.
            return Ok((have, hex(&hasher.finalize())));
        }
        let range = (have > 0).then(|| ("range", format!("bytes={have}-")));
        let resp = self.send_with_stall_timeout("GET", path, range.as_slice(), STALL)?;
        let mut file = if resp.status() == 206 {
            std::fs::OpenOptions::new().append(true).open(dest).map_err(|e| io("opening", e))?
        } else {
            // A whole file (no range asked, or the server ignored it): start over.
            (hasher, have) = (Sha256::new(), 0);
            std::fs::File::create(dest).map_err(|e| io("creating", e))?
        };
        let mut reader = resp.into_reader().take(max_bytes.saturating_sub(have) + 1);
        let mut buf = vec![0u8; 256 * 1024];
        loop {
            let n = reader.read(&mut buf).map_err(|e| ApiError::Transport(format!("the download was interrupted: {e}")))?;
            if n == 0 {
                break;
            }
            have += n as u64;
            if have > max_bytes {
                return Err(ApiError::Transport(format!("{path} is larger than the {max_bytes} bytes expected")));
            }
            hasher.update(&buf[..n]);
            file.write_all(&buf[..n]).map_err(|e| io("writing", e))?;
        }
        file.sync_all().map_err(|e| io("writing", e))?;
        Ok((have, hex(&hasher.finalize())))
    }

    /// A signed request whose answer may take long to read (a big download): instead of
    /// one deadline for the whole request, each read may wait at most `stall`.
    fn send_with_stall_timeout(
        &self,
        method: &str,
        path: &str,
        headers: &[(&str, String)],
        stall: Duration,
    ) -> Result<ureq::Response, ApiError> {
        // ureq's per-request timeout covers the whole body; a separate agent with a
        // read timeout (same TLS settings and pin) only limits the pauses.
        let http = ureq::AgentBuilder::new()
            .tls_config(client_config(self.verifier.clone()))
            .timeout_connect(Duration::from_secs(10))
            .timeout_read(stall)
            .timeout_write(stall)
            .user_agent(&format!("smp-agent/{AGENT_VERSION}"))
            .build();
        self.send_on(&http, method, path, "application/octet-stream", headers, &[], None)
    }

    /// `path` made unique with a request number (`?n=17` or `&n=17`), for requests that
    /// may repeat several times a second (remote control).
    ///
    /// Why: our signatures are deterministic (the same key signing the same message
    /// always gives the same signature, RFC 6979), and the message is method, path,
    /// time (in seconds) and body. Two identical requests within one second would carry
    /// identical signatures, and the server refuses the second as a replay. The number
    /// is part of the signed path, so every request is different.
    pub fn unique(&self, path: &str) -> String {
        let n = self.request_counter.fetch_add(1, Ordering::Relaxed);
        let separator = if path.contains('?') { '&' } else { '?' };
        format!("{path}{separator}n={n}")
    }

    /// A signed GET answered with JSON; `timeout` for the whole request (a long poll
    /// needs more than the server holds it).
    pub fn get_json<R: DeserializeOwned>(&self, path: &str, timeout: Duration) -> Result<R, ApiError> {
        let resp = self.send("GET", path, "application/json", &[], &[], timeout)?;
        resp.into_json::<R>().map_err(|e| ApiError::Transport(format!("bad response: {e}")))
    }

    /// A signed JSON POST the server answers without a body (204), e.g. status reports.
    pub fn post_no_answer<B: Serialize>(&self, path: &str, body: &B) -> Result<(), ApiError> {
        let raw = serde_json::to_vec(body).map_err(|e| ApiError::Transport(e.to_string()))?;
        self.send("POST", path, "application/json", &[], &raw, Duration::from_secs(30))?;
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
        let http = self.http.read().unwrap().clone(); // a cheap handle to the pool
        self.send_on(&http, method, path, content_type, headers, body, Some(timeout))
    }

    /// `send` with a given ureq agent; `timeout`: for the whole request (None: the
    /// agent's own read/write timeouts only).
    #[allow(clippy::too_many_arguments)]
    fn send_on(
        &self,
        http: &ureq::Agent,
        method: &str,
        path: &str,
        content_type: &str,
        headers: &[(&str, String)],
        body: &[u8],
        timeout: Option<Duration>,
    ) -> Result<ureq::Response, ApiError> {
        for attempt in 0..2 {
            let offset_used = self.clock_offset.load(Ordering::Relaxed);
            let ts = unix_now() + offset_used;
            let started = Instant::now();
            let mut request = http
                .request(method, &format!("{}{}", self.server, path))
                .set("content-type", content_type)
                .set("x-smp-key", &self.identity.fingerprint())
                .set("x-smp-time", &ts.to_string())
                .set("x-smp-signature", &self.identity.sign(method, path, ts, body));
            if let Some(timeout) = timeout {
                request = request.timeout(timeout);
            }
            for (name, value) in headers {
                request = request.set(name, value);
            }
            // A GET has no body (an empty one is still what's signed).
            let sent = if method == "GET" { request.call() } else { request.send_bytes(body) };
            match sent {
                Ok(resp) => {
                    self.learn_clock(resp.header("date"), started.elapsed());
                    return Ok(resp);
                }
                Err(ureq::Error::Status(401, resp)) => {
                    // Maybe only our clock is wrong: learn the server's time and retry once.
                    self.learn_clock(resp.header("date"), started.elapsed());
                    let corrected = (self.clock_offset.load(Ordering::Relaxed) - offset_used).abs() > 60;
                    if attempt == 0 && corrected {
                        log::warn!("this computer's clock is off by {} s; retrying with the server's time",
                                   self.clock_offset.load(Ordering::Relaxed));
                        continue;
                    }
                    return Err(ApiError::Unauthorized);
                }
                Err(ureq::Error::Status(status, resp)) => {
                    let retry_after = resp.header("retry-after").and_then(|v| v.trim().parse().ok());
                    // SMP errors look like {"detail": "...", "code": "...", "params": {...}}
                    let text = resp.into_string().unwrap_or_default();
                    let json: Option<serde_json::Value> = serde_json::from_str(&text).ok();
                    let field = |name: &str| json.as_ref().and_then(|j| j.get(name)?.as_str().map(str::to_string));
                    let message = field("detail").unwrap_or_else(|| text.chars().take(300).collect());
                    return Err(ApiError::Http { status, code: field("code"), message, retry_after });
                }
                Err(ureq::Error::Transport(t)) => return Err(ApiError::Transport(t.to_string())),
            }
        }
        Err(ApiError::Unauthorized)
    }

    fn learn_clock(&self, date_header: Option<&str>, took: Duration) {
        if let Some(offset) = clock_offset(date_header, took, unix_now()) {
            self.clock_offset.store(offset, Ordering::Relaxed);
        }
    }
}

fn new_http(verifier: &Arc<PinningVerifier>) -> ureq::Agent {
    ureq::AgentBuilder::new()
        .tls_config(client_config(verifier.clone()))
        .timeout_connect(Duration::from_secs(10))
        // Kept-open connections to the server, for reuse. Several requests run at once
        // (the heartbeat's long poll, and during remote control an input poll and
        // several pictures a second); with ureq's default of one, each picture would
        // need a new TLS handshake.
        .max_idle_connections_per_host(4)
        .user_agent(&format!("smp-agent/{AGENT_VERSION}"))
        .build()
}

/// The server's clock minus ours (seconds), from an answer's Date header, or None when
/// the answer can't tell. Only quick answers count: the server (uvicorn) dates an
/// answer when the request *arrived*, so a long-poll heartbeat held for 25 s says the
/// server is 25 s behind. Believing that, the next heartbeat (often the very same
/// request) was signed with the previous one's time: an identical signature, which the
/// server rightly refuses as a replay, and the agent re-joined every half minute.
/// (nginx in front replaces the Date header with its own, which hid this.)
fn clock_offset(date_header: Option<&str>, took: Duration, now: i64) -> Option<i64> {
    if took > CLOCK_ANSWER_WITHIN {
        return None;
    }
    let server = httpdate::parse_http_date(date_header?).ok()?;
    Some(server.duration_since(UNIX_EPOCH).ok()?.as_secs() as i64 - now)
}

fn unix_now() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unique_paths() {
        let c = Client::new("https://smp.example", Identity::generate(), None);
        assert_eq!(c.unique("/api/x"), "/api/x?n=0");
        assert_eq!(c.unique("/api/x?session=ab"), "/api/x?session=ab&n=1");
    }

    #[test]
    fn join_body_carries_the_building_only_when_set() {
        let mut body = JoinIn {
            public_key: "key".into(),
            hostname: "pc-1",
            mac: None,
            os_name: "Windows",
            os_version: "11",
            agent_version: "1.0.0",
            address: None,
            building: None,
        };
        let json = serde_json::to_value(&body).unwrap();
        assert!(json.get("building").is_none(), "no building: the field is omitted");
        assert_eq!(json["hostname"], "pc-1");

        body.building = Some("Main building".into());
        let json = serde_json::to_value(&body).unwrap();
        assert_eq!(json["building"], "Main building");
    }

    #[test]
    fn the_client_sends_the_building_it_was_given() {
        let c = Client::new("https://smp.example", Identity::generate(), None);
        assert_eq!(*c.building.read().unwrap(), None);
        c.set_building(Some("B".into()));
        assert_eq!(c.building.read().unwrap().as_deref(), Some("B"));
    }

    #[test]
    fn only_quick_answers_set_the_clock() {
        let date = "Tue, 24 Nov 2026 07:19:23 GMT"; // 1795504763
        let quick = Duration::from_millis(80);
        assert_eq!(clock_offset(Some(date), quick, 1_795_504_763), Some(0));
        assert_eq!(clock_offset(Some(date), quick, 1_795_504_863), Some(-100), "our clock is 100 s fast");
        // a long poll held for 25 s: its Date is from when it arrived, not the time now
        assert_eq!(clock_offset(Some(date), Duration::from_secs(25), 1_795_504_788), None);
        assert_eq!(clock_offset(None, quick, 0), None);
        assert_eq!(clock_offset(Some("yesterday"), quick, 0), None);
    }
}
