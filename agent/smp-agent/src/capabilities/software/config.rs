// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! The "software" section of the config, as the server's software module sends it
//! (server/smp/modules/software/__init__.py, agent_entry):
//!
//!   {"packages": [{"id": "...", "name": "7-Zip", "version": "24.08", "action": "install",
//!                  "kind": "msi", "file_name": "7z2408-x64.msi", "size": 1594368,
//!                  "sha256": "...", "url": "/api/v1/software/agent/packages/<id>",
//!                  "install_args": "", "uninstall_command": "",
//!                  "detection": {"type": "msi", "product_code": null},
//!                  "reboot": "never", "success_codes": [], "timeout_s": 3600}],
//!    "status_path": "/api/v1/software/agent/status", "spread_s": 120, "reboot_warning_s": 300}
//!
//! The server already checked all of this; the agent checks the shape again anyway,
//! because it runs what it gets as SYSTEM: a package that doesn't look right is
//! skipped (and logged), never half-understood.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Largest installer we download (the server allows 8 GiB by default).
pub const MAX_SIZE: u64 = 16 * 1024 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SoftwareConfig {
    pub packages: Vec<Package>,
    pub status_path: String,
    #[serde(default)]
    pub spread_s: u64,
    #[serde(default)]
    pub reboot_warning_s: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Action {
    Install,
    Remove,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Msi,
    Exe,
}

impl Kind {
    /// The file extension the downloaded installer gets (Windows looks at it).
    pub fn extension(self) -> &'static str {
        match self {
            Kind::Msi => "msi",
            Kind::Exe => "exe",
        }
    }
}

/// When to restart the computer after installing.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Reboot {
    /// Never; a restart the installer asks for is only reported (reboot_pending).
    Never,
    /// When the installer says it's needed (exit code 3010 or 1641).
    IfRequired,
    /// Always, after a successful installation.
    Always,
}

/// How to tell whether the program is installed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum Detection {
    /// Windows Installer knows this product code. None: read it from the .msi file.
    Msi { product_code: Option<String> },
    /// This key under HKEY_LOCAL_MACHINE exists (64- or 32-bit view); with `value`,
    /// that value exists; with `equals` too, its data is exactly that.
    Registry { key: String, value: Option<String>, equals: Option<String> },
    /// This file exists (%VARIABLES% are expanded).
    File { path: String },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Package {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub version: String,
    pub action: Action,
    pub kind: Kind,
    pub size: u64,
    pub sha256: String,
    pub url: String,
    #[serde(default)]
    pub install_args: String,
    #[serde(default)]
    pub uninstall_command: String,
    pub detection: Detection,
    pub reboot: Reboot,
    #[serde(default)]
    pub success_codes: Vec<i64>,
    pub timeout_s: u64,
}

impl Package {
    /// "7-Zip 24.08" for logs and messages.
    pub fn label(&self) -> String {
        if self.version.is_empty() {
            self.name.clone()
        } else {
            format!("{} {}", self.name, self.version)
        }
    }

    /// Why we won't touch this package, or None if it looks right.
    fn problem(&self) -> Option<&'static str> {
        let hex64 = |s: &str| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase());
        let one_line = |s: &str| !s.chars().any(char::is_control);
        if !self.url.starts_with("/api/v1/software/agent/packages/") || self.url.contains("..") {
            return Some("not a download from SMP's software module");
        }
        if !hex64(&self.sha256) {
            return Some("no valid SHA-256");
        }
        if self.size == 0 || self.size > MAX_SIZE {
            return Some("an impossible size");
        }
        if !self.id.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-') || self.id.is_empty() || self.id.len() > 36 {
            return Some("an invalid id"); // it becomes part of file names
        }
        if !one_line(&self.install_args) || !one_line(&self.uninstall_command) {
            return Some("a command line with control characters");
        }
        if self.kind == Kind::Exe && matches!(self.detection, Detection::Msi { .. }) {
            return Some("a product code rule for a program that isn't an .msi");
        }
        if self.action == Action::Remove && self.kind == Kind::Exe && self.uninstall_command.trim().is_empty() {
            return Some("to be removed, but without an uninstall command");
        }
        None
    }
}

impl SoftwareConfig {
    /// From the "software" section; None when there is none (nothing assigned: stay idle,
    /// and leave every program where it is). Packages that don't look right are dropped.
    pub fn from_section(section: Option<&Value>) -> Option<Self> {
        let section = section?;
        let mut config: Self = match serde_json::from_value(section.clone()) {
            Ok(c) => c,
            Err(e) => {
                log::warn!("software: ignoring a config this agent can't read: {e}");
                return None;
            }
        };
        if !config.status_path.starts_with("/api/") {
            log::warn!("software: ignoring a config with status path {:?}", config.status_path);
            return None;
        }
        config.packages.retain(|p| match p.problem() {
            None => true,
            Some(why) => {
                log::warn!("software: skipping package {} ({:?}): {why}", p.label(), p.id);
                false
            }
        });
        config.spread_s = config.spread_s.min(3600);
        config.reboot_warning_s = config.reboot_warning_s.min(3600);
        for p in &mut config.packages {
            p.timeout_s = p.timeout_s.clamp(60, 6 * 3600);
        }
        Some(config)
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;

    pub fn package(name: &str, action: &str, kind: &str) -> Value {
        serde_json::json!({
            "id": "0b3e3c8e-5f0e-4a43-9a51-5f7f1b2c0a11", "name": name, "version": "1.0", "action": action,
            "kind": kind, "file_name": format!("x.{kind}"), "size": 1000, "sha256": "ab".repeat(32),
            "url": "/api/v1/software/agent/packages/0b3e3c8e-5f0e-4a43-9a51-5f7f1b2c0a11",
            "install_args": "/S", "uninstall_command": "", "detection": {"type": "msi", "product_code": null},
            "reboot": "if_required", "success_codes": [], "timeout_s": 3600
        })
    }

    pub fn section(packages: Vec<Value>) -> Value {
        serde_json::json!({"packages": packages, "status_path": "/api/v1/software/agent/status",
                           "spread_s": 120, "reboot_warning_s": 300})
    }

    #[test]
    fn reads_the_servers_section() {
        let config = SoftwareConfig::from_section(Some(&section(vec![package("7-Zip", "install", "msi")]))).unwrap();
        let p = &config.packages[0];
        assert_eq!((p.action, p.kind, p.reboot), (Action::Install, Kind::Msi, Reboot::IfRequired));
        assert_eq!(p.detection, Detection::Msi { product_code: None });
        assert_eq!(p.label(), "7-Zip 1.0");
        assert_eq!(SoftwareConfig::from_section(None), None);
        assert_eq!(SoftwareConfig::from_section(Some(&serde_json::json!({"x": 1}))), None);
    }

    #[test]
    fn packages_that_dont_look_right_are_skipped() {
        let mut bad = vec![];
        for (field, value) in [
            ("url", serde_json::json!("https://evil.example/x.msi")),
            ("url", serde_json::json!("/api/v1/software/agent/packages/../../x")),
            ("sha256", serde_json::json!("short")),
            ("size", serde_json::json!(0)),
            ("id", serde_json::json!("..\\..\\windows")),
            ("install_args", serde_json::json!("/S\r\ndel x")),
        ] {
            let mut p = package("bad", "install", "msi");
            p[field] = value;
            bad.push(p);
        }
        let mut exe_without_uninstall = package("bad", "remove", "exe");
        exe_without_uninstall["detection"] = serde_json::json!({"type": "file", "path": "C:\\x.exe"});
        bad.push(exe_without_uninstall);
        bad.push(package("good", "install", "msi"));
        let config = SoftwareConfig::from_section(Some(&section(bad))).unwrap();
        assert_eq!(config.packages.iter().map(|p| p.name.as_str()).collect::<Vec<_>>(), ["good"]);
    }

    #[test]
    fn detection_rules() {
        let parse = |v: Value| serde_json::from_value::<Detection>(v).unwrap();
        assert_eq!(
            parse(serde_json::json!({"type": "registry", "key": "HKLM\\SOFTWARE\\X", "value": "V", "equals": "1"})),
            Detection::Registry { key: "HKLM\\SOFTWARE\\X".into(), value: Some("V".into()), equals: Some("1".into()) }
        );
        assert_eq!(
            parse(serde_json::json!({"type": "file", "path": "%ProgramFiles%\\X\\x.exe"})),
            Detection::File { path: "%ProgramFiles%\\X\\x.exe".into() }
        );
    }
}
