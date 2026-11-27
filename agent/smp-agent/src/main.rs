// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

//! smp-agent: the SMP device agent.
//!
//!   smp-agent join --server https://192.168.1.10   ask to be managed, wait for approval
//!       [--building <id or name>]                  (a hint: which building this PC is in)
//!   smp-agent run                                  run in the foreground (development)
//!   smp-agent status                               show identity and server
//!
//! Windows (as administrator):
//!   smp-agent install [--server https://...] [--building <id or name>]
//!                                                  install the service, send the join request
//!   smp-agent uninstall [--purge]                  remove it
//! and, started by Windows / the service only: `service`, `session-helper`.

mod capabilities;
mod core;
mod platform;

use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::AtomicBool;

use anyhow::Result;
use clap::{Parser, Subcommand};

use crate::core::capability::Registry;
use crate::core::runner::Agent;

/// Shown under `--help`: the attribution and notices the license asks for (NOTICE).
const LEGAL: &str = "\
SMP - School Management Portal: https://github.com/ticksparancsnok/SMP
Free software under the GNU Affero General Public License v3 or later; source code at
the address above. This software is based in part on the work of the Independent JPEG Group.";

#[derive(Parser)]
#[command(name = "smp-agent", version = crate::core::api::AGENT_VERSION, about = "SMP device agent", after_help = LEGAL)]
struct Cli {
    /// Where the agent keeps its identity (default: the system location)
    #[arg(long, global = true)]
    state: Option<PathBuf>,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Ask the SMP server to manage this computer, and wait until an admin approves it
    Join {
        /// The SMP address, e.g. https://192.168.1.10 or https://smp.school.example
        #[arg(long)]
        server: String,
        /// Send the request and exit instead of waiting for the approval
        #[arg(long)]
        no_wait: bool,
        /// The building this PC is in: its id or name as SMP shows it. Only a hint, so
        /// that building's manager sees the request; an admin still approves the PC.
        #[arg(long)]
        building: Option<String>,
    },
    /// Run the agent in the foreground (Ctrl+C stops it)
    Run,
    /// Show this computer's identity and server
    Status,
    /// Install (or upgrade) the agent as a Windows service and ask to join SMP
    #[cfg(windows)]
    Install {
        /// The SMP address (asked for when left out)
        #[arg(long)]
        server: Option<String>,
        /// The building this PC is in: its id or name as SMP shows it (asked for when
        /// left out and the PC isn't approved yet). Only a hint for SMP's managers.
        #[arg(long)]
        building: Option<String>,
    },
    /// Stop and remove the Windows service
    #[cfg(windows)]
    Uninstall {
        /// Also delete this computer's identity and logs (C:\ProgramData\SMP)
        #[arg(long)]
        purge: bool,
    },
    /// (Windows' service manager starts the agent with this)
    #[cfg(windows)]
    #[command(hide = true)]
    Service,
    /// (the service starts this in the user's session: screen capture + tray icon)
    #[cfg(windows)]
    #[command(hide = true)]
    SessionHelper {
        #[arg(long)]
        pipe: String,
        /// remote control's input (one way, service -> helper)
        #[arg(long)]
        input_pipe: String,
        #[arg(long)]
        server_pid: u32,
    },
    /// (an update starts this from the old exe: restart the service with the new one,
    /// go back if it doesn't reach the server; platform/win/update.rs)
    #[cfg(windows)]
    #[command(hide = true)]
    UpdateWatch {
        #[arg(long)]
        to: String,
    },
}

fn main() -> ExitCode {
    platform::exe_path(); // remember where we were started from (an update renames the file)
    let cli = Cli::parse();
    init_logging(&cli.command);
    match real_main(cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::FAILURE
        }
    }
}

/// Logs go to the console, except for the Windows service and helper, which have
/// none: they write to C:\ProgramData\SMP\logs. RUST_LOG=debug shows more.
fn init_logging(command: &Command) {
    let mut builder = env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"));
    #[cfg(windows)]
    {
        let file = match command {
            Command::Service => platform::log_file("agent.log"),
            Command::SessionHelper { .. } => platform::log_file("helper.log"),
            Command::UpdateWatch { .. } => platform::log_file("update.log"),
            _ => None,
        };
        if let Some(file) = file {
            builder.target(env_logger::Target::Pipe(Box::new(file)));
        }
    }
    let _ = command;
    builder.init();
    for note in platform::startup_notes() {
        log::warn!("{note}");  // e.g. a C:\ProgramData\SMP someone else made
    }
}

/// The agent itself: capabilities + the join/heartbeat loop, until `stop` is set.
/// Used by `run` (foreground) and by the Windows service.
pub fn run_agent(state_path: &std::path::Path, stop: &AtomicBool) -> Result<()> {
    let mut agent = Agent::open(state_path, None)?;
    core::watchdog::start(agent.contact.clone(), core::watchdog::limit());
    let mut registry = Registry::new(capabilities::all(agent.client.clone(), agent.state_dir()));
    agent.run(&mut registry, stop)
}

fn real_main(cli: Cli) -> Result<()> {
    let state_path = cli.state.unwrap_or_else(core::state::default_path);
    let stop = AtomicBool::new(false);
    match cli.command {
        Command::Join { server, no_wait, building } => {
            if let Some(building) = &building {
                core::state::normalize_building(building)?; // too long: stop before creating anything
            }
            let mut agent = Agent::open(&state_path, Some(&server))?;
            if let Some(building) = &building {
                agent.set_building(building)?;
            }
            if no_wait {
                let r = agent.client.join(&core::sysinfo::SysInfo::collect())?;
                if r.status == "rejected" {
                    println!("{REJECTED_NOTE}");
                } else {
                    show_fingerprint(&r.fingerprint_display);
                }
                return Ok(());
            }
            let mut shown = false;
            agent.wait_until_approved(&stop, |fp| {
                if !shown {
                    show_fingerprint(fp);
                    shown = true;
                }
            })?;
            println!("Approved. Start the agent with: smp-agent run");
        }
        Command::Run => run_agent(&state_path, &stop)?,
        Command::Status => {
            let agent = Agent::open(&state_path, None)?;
            println!("server:       {}", agent.state.server);
            println!("fingerprint:  {}", agent.client.identity().display_fingerprint());
            println!("device id:    {}", agent.state.device_id.as_deref().unwrap_or("(not approved yet)"));
            if let Some(building) = &agent.state.building {
                println!("building:     {building}");
            }
            println!("state file:   {}", state_path.display());
        }
        #[cfg(windows)]
        Command::Install { server, building } => platform::service::install(server, building)?,
        #[cfg(windows)]
        Command::Uninstall { purge } => platform::service::uninstall(purge)?,
        #[cfg(windows)]
        Command::Service => platform::service::run()?,
        #[cfg(windows)]
        Command::SessionHelper { pipe, input_pipe, server_pid } => platform::helper::run(&pipe, &input_pipe, server_pid)?,
        #[cfg(windows)]
        Command::UpdateWatch { to } => platform::update::watch(&to, &state_path)?,
    }
    Ok(())
}

/// What to tell an admin installing on a machine SMP was told to reject.
pub const REJECTED_NOTE: &str = "SMP rejected this computer earlier, so no new request appears. \
An admin can allow it again in SMP: Devices -> Rejected computers -> Allow again.";

pub fn show_fingerprint(fp: &str) {
    println!();
    println!("  Waiting for approval in SMP (Devices -> Waiting for approval).");
    println!("  Approve only if SMP shows this same fingerprint:");
    println!();
    println!("      {fp}");
    println!();
}
