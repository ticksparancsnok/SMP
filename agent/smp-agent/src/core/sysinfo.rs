// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Facts about this computer that the server shows to admins.

use crate::platform;

#[derive(Debug, Clone)]
pub struct SysInfo {
    pub hostname: String,
    pub mac: Option<String>,
    pub os_name: String,
    pub os_version: String,
}

impl SysInfo {
    pub fn collect() -> Self {
        let hostname = gethostname::gethostname().to_string_lossy().into_owned();
        let mac = mac_address::get_mac_address().ok().flatten().map(|m| m.to_string().to_lowercase());
        let (os_name, os_version) = platform::os_info();
        Self { hostname, mac, os_name, os_version }
    }
}

/// The user logged in at the console right now (None: nobody / login screen).
pub fn console_user() -> Option<String> {
    platform::console_user()
}
