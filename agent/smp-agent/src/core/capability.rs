//! Capabilities: what the agent can do (monitor the screen, later: lock it, install
//! software, ...). Each one owns a section of the config the server sends, named
//! after it ("screen": {...}), and runs its own worker thread if it needs one.
//!
//! Adding a capability = a new module under src/capabilities/ implementing this
//! trait, plus one line in capabilities::all(). The core never changes for it.

use serde_json::Value;

pub trait Capability: Send {
    /// The config section this capability reads (also its name in logs).
    fn name(&self) -> &'static str;

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
            cap.apply_config(config.get(cap.name()));
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
}
