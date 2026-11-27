// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Capabilities: what the agent can do (monitor the screen, lock it, install
//! software, ...). Each one owns a section of the config the server sends (by
//! default named after it, see `section`), and runs its own worker thread if it needs one.
//!
//! Adding a capability = a new module under src/capabilities/ implementing this
//! trait, plus one line in capabilities::all(). The core never changes for it.

use serde_json::Value;

pub trait Capability: Send {
    /// The capability's name in logs; also the config section it reads, unless
    /// `section` says otherwise.
    fn name(&self) -> &'static str;

    /// The config section this capability reads: the name of the server module that
    /// sends it. One module may drive several capabilities (the server's "classroom"
    /// module sends both screen monitoring and the screen lock), so this can differ
    /// from `name`; by default it's the same.
    fn section(&self) -> &'static str {
        self.name()
    }

    /// Called whenever the server's config changed; `section` is None when the server
    /// sends nothing for this capability (it should then stop / stay idle).
    fn apply_config(&mut self, section: Option<&Value>);

    /// The agent is shutting down: stop worker threads.
    fn stop(&mut self) {}
}

pub struct Registry {
    capabilities: Vec<Box<dyn Capability>>,
}

impl Registry {
    pub fn new(capabilities: Vec<Box<dyn Capability>>) -> Self {
        Self { capabilities }
    }

    pub fn apply(&mut self, config: &Value) {
        for cap in &mut self.capabilities {
            cap.apply_config(config.get(cap.section()));
        }
    }

    pub fn stop(&mut self) {
        for cap in &mut self.capabilities {
            cap.stop();
        }
    }

    pub fn names(&self) -> Vec<&'static str> {
        self.capabilities.iter().map(|c| c.name()).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    struct Recorder(Arc<Mutex<Vec<Option<Value>>>>);
    impl Capability for Recorder {
        fn name(&self) -> &'static str {
            "screen"
        }
        fn apply_config(&mut self, section: Option<&Value>) {
            self.0.lock().unwrap().push(section.cloned());
        }
    }

    #[test]
    fn each_capability_gets_its_own_section() {
        let seen = Arc::new(Mutex::new(vec![]));
        let mut reg = Registry::new(vec![Box::new(Recorder(seen.clone()))]);
        reg.apply(&serde_json::json!({"agent": {}, "screen": {"interval_s": 5}}));
        reg.apply(&serde_json::json!({"agent": {}}));
        let seen = seen.lock().unwrap();
        assert_eq!(seen[0], Some(serde_json::json!({"interval_s": 5})));
        assert_eq!(seen[1], None);
    }

    /// Named "lock" in logs, reads the "classroom" section (like the real one).
    struct SharedSection(Arc<Mutex<Vec<Option<Value>>>>);
    impl Capability for SharedSection {
        fn name(&self) -> &'static str {
            "lock"
        }
        fn section(&self) -> &'static str {
            "classroom"
        }
        fn apply_config(&mut self, section: Option<&Value>) {
            self.0.lock().unwrap().push(section.cloned());
        }
    }

    #[test]
    fn a_capability_can_read_a_section_with_another_name() {
        let seen = Arc::new(Mutex::new(vec![]));
        let mut reg = Registry::new(vec![Box::new(SharedSection(seen.clone()))]);
        reg.apply(&serde_json::json!({"lock": {"x": 1}, "classroom": {"active": false}}));
        assert_eq!(seen.lock().unwrap()[0], Some(serde_json::json!({"active": false})));
        assert_eq!(reg.names(), ["lock"]);
    }
}
