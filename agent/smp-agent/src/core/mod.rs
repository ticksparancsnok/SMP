// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! Platform-independent agent core (unit-tested on Linux).
pub mod api;
pub mod capability;
pub mod identity;
pub mod presence;
pub mod runner;
pub mod state;
pub mod sysinfo;
pub mod tls;
pub mod update;
pub mod watchdog;
