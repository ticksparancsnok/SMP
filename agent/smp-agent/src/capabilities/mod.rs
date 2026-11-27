// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Everything the agent can do, each in its own module (see core/capability.rs).

pub mod lock;
pub mod remote;
pub mod screen;
pub mod software;
pub mod update;

use std::path::PathBuf;
use std::sync::Arc;

use crate::core::api::Client;
use crate::core::capability::Capability;

/// The capabilities this build runs. Add new ones here. `state_dir`: where agent.json is.
pub fn all(client: Arc<Client>, state_dir: PathBuf) -> Vec<Box<dyn Capability>> {
    vec![
        Box::new(screen::Screen::new(client.clone(), crate::platform::screen_source())),
        Box::new(lock::Lock::new(client.clone(), crate::platform::lock_target())),
        Box::new(remote::Remote::new(client.clone(), crate::platform::remote_screen(), crate::platform::input_sink())),
        Box::new(software::Software::new(client.clone(), state_dir.clone(), crate::platform::software_installer())),
        Box::new(update::Updater::new(client, state_dir)),
    ]
}
