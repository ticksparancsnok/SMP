// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! Which folder the agent keeps its identity and logs in (Windows: under C:\ProgramData).
//!
//! Anyone may create folders in C:\ProgramData, and whoever creates one owns it. A
//! student who makes C:\ProgramData\SMP before the agent is installed would own the
//! agent's folder: an owner can always change its permissions back, read the device key
//! (and the SYSTEM service would be writing into a folder they control). So:
//!
//!   SMP, SMP-1, SMP-2, ...: the first one that is ours (a real folder, not a link,
//!   owned by SYSTEM or Administrators) or doesn't exist yet (then it's created with
//!   that owner and SYSTEM/Administrators-only permissions in one step).
//!
//! The same rule finds the same folder at every start, so an update or restart never
//! moves the agent: its own folder always passes the check. A folder someone else
//! made is skipped and left alone (it is reported in the log).
//!
//! Plain Rust with the file system checks passed in, so it's tested here; the Windows
//! checks are in win/security.rs.

use std::io;
use std::path::{Path, PathBuf};

/// What's at a candidate path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(not(windows), allow(dead_code))]
pub enum Found {
    Missing,
    /// a real folder owned by SYSTEM or Administrators
    Ours,
    /// someone else's folder, a link/junction, or a file
    Foreign,
}

/// How many SMP-n names are tried before giving up.
const MAX_CANDIDATES: usize = 10;

pub fn candidate(base: &Path, n: usize) -> PathBuf {
    if n == 0 {
        base.join("SMP")
    } else {
        base.join(format!("SMP-{n}"))
    }
}

/// The folder to use under `base`, and notes for the log (folders skipped and why).
/// None only if every candidate is taken or can't be created.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn choose(
    base: &Path,
    mut inspect: impl FnMut(&Path) -> Found,
    mut create: impl FnMut(&Path) -> io::Result<()>,
) -> (Option<PathBuf>, Vec<String>) {
    let mut notes = vec![];
    for n in 0..MAX_CANDIDATES {
        let path = candidate(base, n);
        // A few rounds: if creating fails because someone made it just now, look again.
        for _ in 0..3 {
            match inspect(&path) {
                Found::Ours => return (Some(path), notes),
                Found::Foreign => {
                    notes.push(format!(
                        "{} isn't SMP's own (made by someone else, or a link): not using it",
                        path.display()
                    ));
                    break;
                }
                Found::Missing => match create(&path) {
                    Ok(()) => return (Some(path), notes),
                    Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
                    Err(e) => {
                        notes.push(format!("can't create {}: {e}", path.display()));
                        break;
                    }
                },
            }
        }
    }
    (None, notes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::collections::HashMap;

    /// A pretend file system: name -> what's there; creating makes it ours.
    fn run(start: &[(&str, Found)]) -> (Option<String>, usize, HashMap<String, Found>) {
        let fs = RefCell::new(start.iter().map(|(k, v)| (k.to_string(), *v)).collect::<HashMap<_, _>>());
        let name = |p: &Path| p.file_name().unwrap().to_string_lossy().into_owned();
        let (chosen, notes) = choose(
            Path::new("/data"),
            |p| *fs.borrow().get(&name(p)).unwrap_or(&Found::Missing),
            |p| {
                fs.borrow_mut().insert(name(p), Found::Ours);
                Ok(())
            },
        );
        (chosen.map(|p| name(&p)), notes.len(), fs.into_inner())
    }

    #[test]
    fn a_fresh_machine_gets_smp() {
        assert_eq!(run(&[]).0.as_deref(), Some("SMP"));
    }

    #[test]
    fn a_folder_someone_else_made_is_skipped() {
        let (chosen, notes, _) = run(&[("SMP", Found::Foreign)]);
        assert_eq!((chosen.as_deref(), notes), (Some("SMP-1"), 1));
        let (chosen, ..) = run(&[("SMP", Found::Foreign), ("SMP-1", Found::Foreign)]);
        assert_eq!(chosen.as_deref(), Some("SMP-2"));
    }

    #[test]
    fn every_start_finds_the_same_folder() {
        // first start: SMP was squatted, SMP-1 is created and ours from then on
        let (first, _, fs) = run(&[("SMP", Found::Foreign)]);
        let state: Vec<_> = fs.into_iter().collect();
        let refs: Vec<(&str, Found)> = state.iter().map(|(k, v)| (k.as_str(), *v)).collect();
        // restart / update: the same one, not SMP-2
        assert_eq!(run(&refs).0, first);
        // our own SMP (the normal case) stays SMP
        assert_eq!(run(&[("SMP", Found::Ours)]).0.as_deref(), Some("SMP"));
    }

    #[test]
    fn someone_creating_it_at_the_same_moment_is_handled() {
        let tries = RefCell::new(0);
        let (chosen, _) = choose(
            Path::new("/data"),
            |p| match (p.ends_with("SMP"), *tries.borrow()) {
                (true, 0) => Found::Missing,
                (true, _) => Found::Foreign, // a student made it between the look and the create
                (false, _) => Found::Missing,
            },
            |p| {
                if p.ends_with("SMP") {
                    *tries.borrow_mut() += 1;
                    Err(io::Error::from(io::ErrorKind::AlreadyExists))
                } else {
                    Ok(())
                }
            },
        );
        assert_eq!(chosen, Some(PathBuf::from("/data/SMP-1")));
    }
}
