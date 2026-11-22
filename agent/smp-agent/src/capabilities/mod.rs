// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Everything the agent can do, each in its own module (see core/capability.rs).

pub mod screen;

use std::sync::Arc;

use crate::core::api::Client;
use crate::core::capability::Capability;

/// The capabilities this build runs. Add new ones here.
pub fn all(client: Arc<Client>) -> Vec<Box<dyn Capability>> {
    vec![Box::new(screen::Screen::new(client, crate::platform::screen_source()))]
}
