// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Telling SMP how each package is doing (POST <status_path>, see the server's
//! software module). Only changes are sent, and what couldn't be sent (server away)
//! is sent with the next try. After an agent restart everything is sent once again.

use std::collections::HashMap;

use serde::Serialize;

use crate::core::api::Client;

/// The server keeps at most 500 characters.
const MAX_MESSAGE_CHARS: usize = 450;

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Report {
    pub package_id: String,
    /// "waiting" | "downloading" | "installing" | "installed" | "reboot_pending" |
    /// "failed" | "removing" | "removed" | "remove_failed"
    pub state: &'static str,
    /// The file this is about (the server shows "pending" for reports on older files).
    pub sha256: Option<String>,
    pub exit_code: Option<i64>,
    pub message: String,
}

impl Report {
    pub fn new(package_id: &str, state: &'static str, sha256: &str) -> Self {
        Self { package_id: package_id.into(), state, sha256: Some(sha256.into()), exit_code: None, message: String::new() }
    }

    pub fn with(mut self, exit_code: Option<i64>, message: &str) -> Self {
        self.exit_code = exit_code;
        self.message = message.chars().filter(|c| !c.is_control()).take(MAX_MESSAGE_CHARS).collect();
        self
    }
}

#[derive(Default)]
pub struct Reporter {
    /// What the server has, per package.
    sent: HashMap<String, Report>,
    /// Changes not sent yet (at most one per package: the newest).
    queue: Vec<Report>,
}

#[derive(Serialize)]
struct Body<'a> {
    reports: &'a [Report],
}

impl Reporter {
    /// Note a package's state; queued only if it differs from what the server has.
    pub fn set(&mut self, report: Report) {
        self.queue.retain(|q| q.package_id != report.package_id);
        if self.sent.get(&report.package_id) != Some(&report) {
            self.queue.push(report);
        }
    }

    /// Send what's queued. Failures are only logged: the queue stays for next time.
    pub fn flush(&mut self, client: &Client, status_path: &str) {
        if self.queue.is_empty() {
            return;
        }
        match client.post_no_answer(status_path, &Body { reports: &self.queue }) {
            Ok(()) => {
                for r in self.queue.drain(..) {
                    self.sent.insert(r.package_id.clone(), r);
                }
            }
            Err(e) => log::warn!("software: couldn't report to SMP ({e}); trying again later"),
        }
    }

    #[cfg(test)]
    pub fn queued(&self) -> &[Report] {
        &self.queue
    }

    #[cfg(test)]
    pub fn mark_sent(&mut self) {
        for r in self.queue.drain(..) {
            self.sent.insert(r.package_id.clone(), r);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_changes_are_sent() {
        let sha = "a".repeat(64);
        let mut r = Reporter::default();
        r.set(Report::new("p", "downloading", &sha));
        r.set(Report::new("p", "installing", &sha));
        assert_eq!(r.queued().iter().map(|q| q.state).collect::<Vec<_>>(), ["installing"], "the newest per package");
        r.mark_sent();
        r.set(Report::new("p", "installing", &sha));
        assert!(r.queued().is_empty(), "nothing new");
        r.set(Report::new("p", "failed", &sha).with(Some(1603), "fatal\r\nerror"));
        assert_eq!(r.queued()[0].message, "fatalerror");
        assert_eq!(r.queued()[0].exit_code, Some(1603));
    }
}
