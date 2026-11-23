// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Everything the agent can do, each in its own module (see core/capability.rs).

pub mod screen;
pub mod update;

use std::path::PathBuf;
use std::sync::Arc;

use crate::core::api::Client;
use crate::core::capability::Capability;

/// The capabilities this build runs. Add new ones here. `state_dir`: where agent.json is.
pub fn all(client: Arc<Client>, state_dir: PathBuf) -> Vec<Box<dyn Capability>> {
    vec![
        Box::new(screen::Screen::new(client.clone(), crate::platform::screen_source())),
        Box::new(update::Updater::new(client, state_dir)),
    ]
}
