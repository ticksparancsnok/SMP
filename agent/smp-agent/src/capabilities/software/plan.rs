// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! The decisions, without doing anything: what to do with a package, which command
//! line runs, what an installer's exit code means. Kept free of I/O so it's all
//! unit-tested on Linux (the doing is in mod.rs and the platform).

use std::path::Path;

use super::config::{Action, Kind, Package};

/// What a package needs on this computer, given whether it's installed now.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step {
    /// Installed, and it's the file we installed ourselves: nothing to do.
    UpToDate,
    /// Installed, but not by us (by hand, an image, another tool): take it as it is.
    Adopt,
    /// Not installed, or we installed an older file of this package: install this one.
    Install,
    /// To be removed and still there.
    Remove,
    /// To be removed and not there (any more).
    Gone,
}

/// `installed`: what the detection rule says now. `installed_sha256`: the file this
/// agent last installed for the package (None: never, or forgotten after removal).
pub fn decide(action: Action, installed: bool, installed_sha256: Option<&str>, current_sha256: &str) -> Step {
    match (action, installed) {
        (Action::Install, false) => Step::Install,
        (Action::Install, true) => match installed_sha256 {
            Some(sha) if sha == current_sha256 => Step::UpToDate,
            // A new file for the package = a new version: install it over the old one.
            // (The detection rule may well find the old version, e.g. a file rule.)
            Some(_) => Step::Install,
            None => Step::Adopt,
        },
        (Action::Remove, true) => Step::Remove,
        (Action::Remove, false) => Step::Gone,
    }
}

/// A program to start: `args` are quoted the normal way (paths with spaces), `raw` is
/// the admin's text, passed on exactly as typed (installers parse their own command
/// lines, often not the way Windows quoting expects). No shell is involved, so `raw`
/// can't run other commands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Invocation {
    /// May contain %VARIABLES% (the platform expands them).
    pub program: String,
    pub args: Vec<String>,
    pub raw: String,
}

/// Windows Installer, quietly and never restarting by itself (SMP decides that), with a
/// verbose log for when something fails.
pub const MSIEXEC: &str = r"%SystemRoot%\System32\msiexec.exe";

pub fn install_invocation(p: &Package, file: &Path, log: &Path) -> Invocation {
    match p.kind {
        Kind::Msi => Invocation {
            program: MSIEXEC.into(),
            args: vec![
                "/i".into(),
                file.display().to_string(),
                "/qn".into(),
                "/norestart".into(),
                "/l*v".into(),
                log.display().to_string(),
            ],
            raw: p.install_args.trim().to_string(),
        },
        Kind::Exe => Invocation { program: file.display().to_string(), args: vec![], raw: p.install_args.trim().to_string() },
    }
}

/// How to remove it: `msiexec /x {product code}` for an .msi, the admin's uninstall
/// command for an .exe. Err: there's no way (no product code / no command).
pub fn uninstall_invocation(p: &Package, product_code: Option<&str>, log: &Path) -> Result<Invocation, String> {
    match p.kind {
        Kind::Msi => {
            let code = product_code.ok_or("the package's product code is unknown")?;
            Ok(Invocation {
                program: MSIEXEC.into(),
                args: vec!["/x".into(), code.into(), "/qn".into(), "/norestart".into(), "/l*v".into(), log.display().to_string()],
                raw: String::new(),
            })
        }
        Kind::Exe => {
            let (program, raw) = split_command(&p.uninstall_command).ok_or("the package has no uninstall command")?;
            Ok(Invocation { program, args: vec![], raw })
        }
    }
}

/// `"C:\Program Files\X\uninst.exe" /S` -> (`C:\Program Files\X\uninst.exe`, `/S`):
/// the program is the quoted part, or everything up to the first space.
pub fn split_command(command: &str) -> Option<(String, String)> {
    let command = command.trim();
    let (program, rest) = if let Some(quoted) = command.strip_prefix('"') {
        let end = quoted.find('"')?;
        (&quoted[..end], &quoted[end + 1..])
    } else {
        command.split_once(' ').unwrap_or((command, ""))
    };
    let program = program.trim();
    (!program.is_empty()).then(|| (program.to_string(), rest.trim().to_string()))
}

/// What an installer's exit code means.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    Done { restart_needed: bool },
    /// Another installation is running (Windows Installer allows one at a time): later.
    Busy,
    Failed(String),
}

/// `code` as the process reported it (Windows exit codes are 32-bit; tools show them
/// signed or unsigned, so the admin's `success_codes` may be either).
pub fn outcome(code: i32, success_codes: &[i64]) -> Outcome {
    let unsigned = i64::from(code as u32);
    match unsigned {
        0 => Outcome::Done { restart_needed: false },
        // ERROR_SUCCESS_REBOOT_REQUIRED, ERROR_SUCCESS_REBOOT_INITIATED
        3010 | 1641 => Outcome::Done { restart_needed: true },
        1618 => Outcome::Busy, // ERROR_INSTALL_ALREADY_RUNNING
        _ if success_codes.iter().any(|&c| c == unsigned || c == i64::from(code)) => {
            Outcome::Done { restart_needed: false }
        }
        _ => Outcome::Failed(describe(unsigned)),
    }
}

/// The exit code in words, for the ones Windows Installer (and many setup programs) use.
fn describe(code: i64) -> String {
    let meaning = match code {
        1602 => "the installation was cancelled",
        1603 => "fatal error during installation",
        1605 => "this product isn't installed",
        1619 => "the installation package couldn't be opened",
        1620 => "the installation package isn't valid",
        1625 => "a system policy forbids this installation",
        1633 => "this package isn't supported on this platform (32/64-bit?)",
        1638 => "another version of this product is already installed",
        1639 => "invalid command line arguments",
        _ => "",
    };
    if meaning.is_empty() {
        format!("exit code {code}")
    } else {
        format!("exit code {code}: {meaning}")
    }
}

/// How long to wait before trying a failed package again: 1 h, 2 h, 4 h, ... at most a
/// day. Failing installers shouldn't download hundreds of MB every half hour, and an
/// admin who fixes the package (a changed recipe) gets a retry at once anyway.
pub fn retry_after_failure(attempts: u32) -> std::time::Duration {
    let hours = 1u64 << attempts.saturating_sub(1).min(5);
    std::time::Duration::from_secs(hours.min(24) * 3600)
}

/// Does the computer restart after this installation? (`restart_needed`: the
/// installer said so.)
pub fn wants_restart(p: &Package, action: Action, restart_needed: bool) -> bool {
    use super::config::Reboot;
    match (p.reboot, action) {
        (Reboot::Never, _) => false,
        (Reboot::Always, Action::Install) => true,
        // A removal that needs a restart follows the package's choice, "always" too.
        (Reboot::Always | Reboot::IfRequired, _) => restart_needed,
    }
}

#[cfg(test)]
mod tests {
    use super::super::config::tests::package;
    use super::super::config::Reboot;
    use super::*;

    fn pkg(kind: &str, f: impl FnOnce(&mut serde_json::Value)) -> Package {
        let mut v = package("X", "install", kind);
        if kind == "exe" {
            v["detection"] = serde_json::json!({"type": "file", "path": "C:\\x.exe"});
        }
        f(&mut v);
        serde_json::from_value(v).unwrap()
    }

    #[test]
    fn what_to_do() {
        let (new, old) = ("a".repeat(64), "b".repeat(64));
        assert_eq!(decide(Action::Install, false, None, &new), Step::Install);
        assert_eq!(decide(Action::Install, false, Some(&new), &new), Step::Install, "someone uninstalled it");
        assert_eq!(decide(Action::Install, true, Some(&new), &new), Step::UpToDate);
        assert_eq!(decide(Action::Install, true, Some(&old), &new), Step::Install, "a new version");
        assert_eq!(decide(Action::Install, true, None, &new), Step::Adopt);
        assert_eq!(decide(Action::Remove, true, None, &new), Step::Remove);
        assert_eq!(decide(Action::Remove, false, Some(&new), &new), Step::Gone);
    }

    #[test]
    fn command_lines() {
        let msi = pkg("msi", |v| v["install_args"] = "ALLUSERS=1 INSTALLDIR=\"C:\\Program Files\\X\"".into());
        let inv = install_invocation(&msi, Path::new(r"C:\ProgramData\SMP\software\ab.msi"), Path::new(r"C:\l.log"));
        assert_eq!(inv.program, MSIEXEC);
        assert_eq!(inv.args, ["/i", r"C:\ProgramData\SMP\software\ab.msi", "/qn", "/norestart", "/l*v", r"C:\l.log"]);
        assert_eq!(inv.raw, "ALLUSERS=1 INSTALLDIR=\"C:\\Program Files\\X\"");
        let un = uninstall_invocation(&msi, Some("{11111111-2222-3333-4444-555555555555}"), Path::new("l")).unwrap();
        assert_eq!(un.args[..2], ["/x", "{11111111-2222-3333-4444-555555555555}"]);
        assert!(uninstall_invocation(&msi, None, Path::new("l")).is_err());

        let exe = pkg("exe", |v| v["uninstall_command"] = r#""%ProgramFiles%\VLC\uninstall.exe" /S /NCRC"#.into());
        let inv = install_invocation(&exe, Path::new(r"C:\x\vlc.exe"), Path::new("l"));
        assert_eq!((inv.program.as_str(), inv.raw.as_str()), (r"C:\x\vlc.exe", "/S"));
        let un = uninstall_invocation(&exe, None, Path::new("l")).unwrap();
        assert_eq!((un.program.as_str(), un.raw.as_str()), (r"%ProgramFiles%\VLC\uninstall.exe", "/S /NCRC"));
    }

    #[test]
    fn splitting_uninstall_commands() {
        assert_eq!(split_command(r"C:\x\un.exe /S"), Some((r"C:\x\un.exe".into(), "/S".into())));
        assert_eq!(split_command(r#" "C:\a b\un.exe"  --quiet "#), Some((r"C:\a b\un.exe".into(), "--quiet".into())));
        assert_eq!(split_command(r"C:\x\un.exe"), Some((r"C:\x\un.exe".into(), "".into())));
        assert_eq!(split_command(r#""C:\unterminated"#), None);
        assert_eq!(split_command("  "), None);
    }

    #[test]
    fn exit_codes() {
        assert_eq!(outcome(0, &[]), Outcome::Done { restart_needed: false });
        assert_eq!(outcome(3010, &[]), Outcome::Done { restart_needed: true });
        assert_eq!(outcome(1641, &[]), Outcome::Done { restart_needed: true });
        assert_eq!(outcome(1618, &[]), Outcome::Busy);
        assert_eq!(outcome(1603, &[]), Outcome::Failed("exit code 1603: fatal error during installation".into()));
        assert_eq!(outcome(1, &[1]), Outcome::Done { restart_needed: false });
        // 0x80070005 as Windows reports it (negative as i32), listed either way
        let access_denied = 0x8007_0005u32 as i32;
        assert_eq!(outcome(access_denied, &[0x8007_0005]), Outcome::Done { restart_needed: false });
        assert_eq!(outcome(access_denied, &[i64::from(access_denied)]), Outcome::Done { restart_needed: false });
        assert_eq!(outcome(access_denied, &[]), Outcome::Failed("exit code 2147942405".into()));
    }

    #[test]
    fn restarts_and_retries() {
        let p = |r: Reboot| pkg("msi", |v| v["reboot"] = serde_json::to_value(r).unwrap());
        assert!(!wants_restart(&p(Reboot::Never), Action::Install, true));
        assert!(wants_restart(&p(Reboot::IfRequired), Action::Install, true));
        assert!(!wants_restart(&p(Reboot::IfRequired), Action::Install, false));
        assert!(wants_restart(&p(Reboot::Always), Action::Install, false));
        assert!(!wants_restart(&p(Reboot::Always), Action::Remove, false));
        let hours: Vec<u64> = (1..=8).map(|n| retry_after_failure(n).as_secs() / 3600).collect();
        assert_eq!(hours, [1, 2, 4, 8, 16, 24, 24, 24]);
    }
}
