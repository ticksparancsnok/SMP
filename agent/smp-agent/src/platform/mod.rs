// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Operating-system specific pieces behind one small interface. The core only calls
//! these functions; the Windows implementations live in win/, everything else
//! (Linux, used for development and tests) in generic.rs.

#[cfg(not(windows))]
mod generic;
#[cfg(not(windows))]
pub use generic::*;

#[cfg(windows)]
pub mod win;
#[cfg(windows)]
pub use win::*;

/// How the Windows service and its session helper talk (plain Rust, tested on Linux).
#[cfg(any(windows, test))]
pub mod helper_protocol;

/// Which folder the agent's identity and logs go in (Windows; tested on Linux).
#[cfg(any(windows, test))]
pub mod state_dir;

/// This program's path as it was started. main() asks first thing: an update renames
/// the running file later, and the path the OS reports may then change with it.
pub fn exe_path() -> std::path::PathBuf {
    static EXE: std::sync::OnceLock<std::path::PathBuf> = std::sync::OnceLock::new();
    EXE.get_or_init(|| std::env::current_exe().unwrap_or_else(|_| std::path::PathBuf::from("smp-agent"))).clone()
}
