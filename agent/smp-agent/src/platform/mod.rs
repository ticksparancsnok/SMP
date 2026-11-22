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
