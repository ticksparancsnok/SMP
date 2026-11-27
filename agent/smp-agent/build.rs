// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Compiles the agent's version in from Version.toml (the repository root; the only
//! place SMP's versions are written): `env!("SMP_AGENT_VERSION")` = `[agent] version`.
//! SMP_VERSION_FILE overrides where the file is.
//!
//! Cargo.toml has no version of its own (Cargo then uses 0.0.0 internally; the crate is
//! never published). Line-based reading, like tools/version.sh, so no TOML crate is needed:
//! keep `version = "..."` on one line under `[agent]`.

use std::path::PathBuf;
use std::{env, fs};

fn main() {
    println!("cargo:rerun-if-env-changed=SMP_VERSION_FILE");
    let path = match env::var_os("SMP_VERSION_FILE") {
        Some(p) => PathBuf::from(p),
        // agent/smp-agent -> repository root
        None => PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("set by cargo")).join("../../Version.toml"),
    };
    println!("cargo:rerun-if-changed={}", path.display());

    let text = fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "can't read {} ({e}): the agent's version comes from Version.toml in the SMP \
             repository root; build inside the repository or set SMP_VERSION_FILE",
            path.display()
        )
    });
    let version = agent_version(&text)
        .unwrap_or_else(|| panic!("{}: no `version = \"...\"` under [agent]", path.display()));
    // The server stores it in a 32-character column; Version.toml's own rules.
    assert!(
        !version.is_empty() && version.len() <= 32 && !version.contains(['"', '\\']),
        "{}: [agent] version {version:?} is not like \"1.0.3\" or \"1.0.3 A\"",
        path.display()
    );
    println!("cargo:rustc-env=SMP_AGENT_VERSION={version}");
}

/// `version = "..."` of the `[agent]` section; a comment may follow the header.
fn agent_version(text: &str) -> Option<&str> {
    let mut in_agent = false;
    for line in text.lines().map(str::trim) {
        if let Some(header) = line.strip_prefix('[') {
            in_agent = header.split(']').next().map(str::trim) == Some("agent");
        } else if in_agent {
            if let Some(value) = line.strip_prefix("version").map(str::trim_start).and_then(|r| r.strip_prefix('=')) {
                return value.trim().strip_prefix('"')?.split('"').next();
            }
        }
    }
    None
}
