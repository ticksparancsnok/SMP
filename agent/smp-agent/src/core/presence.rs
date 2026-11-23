// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Changes the server should hear about at once, not at the next regular heartbeat:
//! who is logged in, and this computer's network address (a laptop moved to another
//! Wi-Fi/VLAN, the DHCP lease changed). The main loop checks every second.

use std::net::{IpAddr, SocketAddr, ToSocketAddrs, UdpSocket};
use std::time::{Duration, Instant};

use crate::core::sysinfo::console_user;

#[derive(Debug, Clone, PartialEq)]
pub struct Presence {
    pub user: Option<String>,
    /// The local address used to reach the server (None: no network).
    pub address: Option<IpAddr>,
}

pub struct PresenceProbe {
    /// "host:port" of the SMP server
    server: String,
    /// The server's resolved address (looked up again after a failure).
    target: Option<SocketAddr>,
    /// The last name lookup. While the server is away its name may not resolve (often
    /// SMP itself is the school's DNS), and each try can block for seconds; the main
    /// loop samples every second, so look it up again at most every LOOKUP_EVERY.
    last_lookup: Option<Instant>,
}

const LOOKUP_EVERY: Duration = Duration::from_secs(30);

impl PresenceProbe {
    pub fn new(server_url: &str) -> Self {
        Self { server: host_port(server_url), target: None, last_lookup: None }
    }

    pub fn sample(&mut self) -> Presence {
        Presence { user: console_user(), address: self.local_address() }
    }

    /// Which of our addresses the system would use to reach the server. A UDP
    /// "connect" only picks the route; no packet is sent. This is also the address the
    /// agent reports as the computer's own: right even behind a NAT (e.g. a lab with its
    /// own router), where the server only sees the router.
    pub fn local_address(&mut self) -> Option<IpAddr> {
        if self.target.is_none() {
            if self.last_lookup.is_some_and(|t| t.elapsed() < LOOKUP_EVERY) {
                return None;
            }
            self.last_lookup = Some(Instant::now());
            self.target = self.server.to_socket_addrs().ok()?.next();
        }
        let target = self.target?;
        let any: SocketAddr = if target.is_ipv4() { ([0, 0, 0, 0], 0).into() } else { ([0u16; 8], 0).into() };
        let socket = UdpSocket::bind(any).ok()?;
        if socket.connect(target).is_err() {
            self.target = None; // no route (network down?): resolve the name again next time
            return None;
        }
        socket.local_addr().ok().map(|a| a.ip())
    }
}

impl Presence {
    /// For the log: what changed.
    pub fn describe_change(&self, before: &Presence) -> String {
        let mut parts = vec![];
        if self.user != before.user {
            parts.push(format!("user {:?} -> {:?}", before.user, self.user));
        }
        if self.address != before.address {
            let show = |a: &Option<IpAddr>| a.map(|a| a.to_string()).unwrap_or_else(|| "none".into());
            parts.push(format!("address {} -> {}", show(&before.address), show(&self.address)));
        }
        parts.join(", ")
    }
}

/// "https://smp.school:8443/x" -> "smp.school:8443"; "https://10.0.0.5" -> "10.0.0.5:443"
fn host_port(url: &str) -> String {
    let rest = url.split_once("://").map(|(_, r)| r).unwrap_or(url);
    let authority = rest.split('/').next().unwrap_or(rest);
    let has_port = match authority.rfind(']') {
        Some(end) => authority[end..].contains(':'), // [IPv6]:port
        None => authority.contains(':'),
    };
    if has_port { authority.to_string() } else { format!("{authority}:443") }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_addresses() {
        assert_eq!(host_port("https://192.168.1.10:8443"), "192.168.1.10:8443");
        assert_eq!(host_port("https://smp.iskola.hu/"), "smp.iskola.hu:443");
        assert_eq!(host_port("https://[fd00::5]"), "[fd00::5]:443");
        assert_eq!(host_port("https://[fd00::5]:8443/x"), "[fd00::5]:8443");
    }

    #[test]
    fn finds_the_local_address() {
        let mut probe = PresenceProbe::new("https://127.0.0.1:9");
        assert_eq!(probe.local_address(), Some(IpAddr::from([127, 0, 0, 1])));
        let a = Presence { user: Some("anna".into()), address: None };
        let b = Presence { user: None, address: Some(IpAddr::from([10, 0, 0, 7])) };
        assert_eq!(b.describe_change(&a), "user Some(\"anna\") -> None, address none -> 10.0.0.7");
    }
}
