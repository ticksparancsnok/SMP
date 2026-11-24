// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! The last line of defence against an agent that doesn't reconnect after the server
//! was away, until someone restarts it: if nothing has reached the server for a while,
//! restart the agent process, which is what that someone would do.
//!
//! The main loop already retries every error (runner.rs), and gives up on requests that
//! hang; this catches whatever is left (a stuck thread, a wedged network stack, a state
//! we didn't foresee). It runs on its own thread, so it fires even if the loop itself is
//! stuck. A server that is simply down for hours costs a restart every 15 minutes, a few
//! seconds each.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use crate::core::update::unix_now;

const DEFAULT_LIMIT: Duration = Duration::from_secs(15 * 60);

/// When the server last answered (unix seconds); shared by the loop and the watchdog.
#[derive(Clone)]
pub struct Contact(Arc<AtomicU64>);

impl Default for Contact {
    fn default() -> Self {
        Self::new()
    }
}

impl Contact {
    /// Counting from now (a fresh process gets the full time to connect).
    pub fn new() -> Self {
        Self(Arc::new(AtomicU64::new(unix_now())))
    }

    pub fn touch(&self) {
        self.0.store(unix_now(), Ordering::Relaxed);
    }

    pub fn seconds_since(&self) -> u64 {
        unix_now().saturating_sub(self.0.load(Ordering::Relaxed))
    }
}

/// SMP_AGENT_WATCHDOG_SECONDS overrides the 15 minutes (tests; at least 30 s).
pub fn limit() -> Duration {
    std::env::var("SMP_AGENT_WATCHDOG_SECONDS")
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .map(|s| Duration::from_secs(s.max(30)))
        .unwrap_or(DEFAULT_LIMIT)
}

/// Watch `contact`; past `limit` without one, restart the process (never returns then).
pub fn start(contact: Contact, limit: Duration) {
    let check_every = (limit / 6).clamp(Duration::from_secs(5), Duration::from_secs(30));
    std::thread::Builder::new()
        .name("watchdog".into())
        .spawn(move || loop {
            std::thread::sleep(check_every);
            let since = contact.seconds_since();
            if since > limit.as_secs() {
                log::error!("no answer from the server for {since} s; restarting the agent (watchdog)");
                crate::platform::restart_self();
            }
        })
        .expect("starting the watchdog thread");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn contact_counts_from_the_last_touch() {
        let c = Contact::new();
        assert!(c.seconds_since() <= 1);
        c.0.store(unix_now() - 100, Ordering::Relaxed);
        assert!((100..=101).contains(&c.seconds_since()));
        c.touch();
        assert!(c.seconds_since() <= 1);
    }
}
