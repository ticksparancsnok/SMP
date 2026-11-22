// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

//! smp-agent: the SMP device agent.
//!
//!   smp-agent join --server https://192.168.1.10   ask to be managed, wait for approval
//!   smp-agent run                                  run in the foreground (development)
//!   smp-agent status                               show identity and server
//!
//! Windows (as administrator):
//!   smp-agent install [--server https://...]       install the service, send the join request
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
#[command(name = "smp-agent", version, about = "SMP device agent", after_help = LEGAL)]
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
        #[arg(long)]
        server_pid: u32,
    },
}

fn main() -> ExitCode {
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
            _ => None,
        };
        if let Some(file) = file {
            builder.target(env_logger::Target::Pipe(Box::new(file)));
        }
    }
    let _ = command;
    builder.init();
}

/// The agent itself: capabilities + the join/heartbeat loop, until `stop` is set.
/// Used by `run` (foreground) and by the Windows service.
pub fn run_agent(state_path: &std::path::Path, stop: &AtomicBool) -> Result<()> {
    let mut agent = Agent::open(state_path, None)?;
    let mut registry = Registry::new(capabilities::all(agent.client.clone()));
    agent.run(&mut registry, stop)
}

fn real_main(cli: Cli) -> Result<()> {
    let state_path = cli.state.unwrap_or_else(core::state::default_path);
    let stop = AtomicBool::new(false);
    match cli.command {
        Command::Join { server, no_wait } => {
            let mut agent = Agent::open(&state_path, Some(&server))?;
            if no_wait {
                let r = agent.client.join(&core::sysinfo::SysInfo::collect())?;
                show_fingerprint(&r.fingerprint_display);
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
            println!("state file:   {}", state_path.display());
        }
        #[cfg(windows)]
        Command::Install { server } => platform::service::install(server)?,
        #[cfg(windows)]
        Command::Uninstall { purge } => platform::service::uninstall(purge)?,
        #[cfg(windows)]
        Command::Service => platform::service::run()?,
        #[cfg(windows)]
        Command::SessionHelper { pipe, server_pid } => platform::helper::run(&pipe, server_pid)?,
    }
    Ok(())
}

pub fn show_fingerprint(fp: &str) {
    println!();
    println!("  Waiting for approval in SMP (Devices -> Waiting for approval).");
    println!("  Approve only if SMP shows this same fingerprint:");
    println!();
    println!("      {fp}");
    println!();
}
