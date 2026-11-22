//! The Windows service.
//!
//!   smp-agent install [--server https://...]   (as administrator) copy the exe to
//!       C:\Program Files\SMP, create the identity, send the join request, register
//!       and start the "SMPAgent" service. Running it again upgrades the exe.
//!   smp-agent uninstall [--purge]               stop and remove the service
//!                                               (--purge: also forget the identity)
//!
//! The service runs as SYSTEM and starts with Windows. If it crashes or exits with
//! an error, Windows restarts it after 5 seconds.

use std::ffi::{OsStr, OsString};
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use windows_service::service::{
    ServiceAccess, ServiceAction, ServiceActionType, ServiceControl, ServiceControlAccept, ServiceErrorControl,
    ServiceExitCode, ServiceFailureActions, ServiceFailureResetPeriod, ServiceInfo, ServiceStartType, ServiceState,
    ServiceStatus, ServiceType,
};
use windows_service::service_control_handler::{self, ServiceControlHandlerResult, ServiceStatusHandle};
use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
use windows_service::{define_windows_service, service_dispatcher};

use crate::core::runner::Agent;
use crate::core::state;
use crate::core::sysinfo::SysInfo;

pub const SERVICE_NAME: &str = "SMPAgent";
const DISPLAY_NAME: &str = "SMP Agent";
const DESCRIPTION: &str = "SMP school management: lets the school's SMP server manage this computer \
                           (screen monitoring while a teacher watches the room; the tray icon shows when).";

/// Set when Windows asks the service to stop.
static STOP: AtomicBool = AtomicBool::new(false);

// ------------------------------------------------------------------ install / uninstall

pub fn install(server: Option<String>) -> Result<()> {
    let server = match server {
        Some(s) => s,
        None => ask("SMP server address (e.g. https://192.168.1.10): ")?,
    };
    let server = normalize_server(&server)?;

    let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE)
        .context("the installer must run as administrator (right click -> Run as administrator)")?;

    // Upgrading: stop the running service first, so its exe can be replaced.
    let existing = manager.open_service(SERVICE_NAME, ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::START | ServiceAccess::CHANGE_CONFIG).ok();
    if let Some(service) = &existing {
        stop_and_wait(service)?;
    }

    let exe = install_exe()?;

    // The identity, and the join request (an admin approves it in SMP -> Devices).
    let state_path = state::default_path();
    let agent = Agent::open(&state_path, Some(&server))?;
    match agent.client.join(&SysInfo::collect()) {
        Ok(reply) if reply.status == "approved" => println!("This computer is already approved in SMP."),
        Ok(reply) => crate::show_fingerprint(&reply.fingerprint_display),
        Err(e) => println!("Couldn't reach {server} now ({e}); the service will keep trying."),
    }

    let info = ServiceInfo {
        name: OsString::from(SERVICE_NAME),
        display_name: OsString::from(DISPLAY_NAME),
        service_type: ServiceType::OWN_PROCESS,
        start_type: ServiceStartType::AutoStart,
        error_control: ServiceErrorControl::Normal,
        executable_path: exe.clone(),
        launch_arguments: vec![OsString::from("service")],
        dependencies: vec![],
        account_name: None, // LocalSystem
        account_password: None,
    };
    let service = match existing {
        Some(service) => {
            service.change_config(&info).context("updating the service")?;
            service
        }
        None => manager
            .create_service(&info, ServiceAccess::CHANGE_CONFIG | ServiceAccess::START | ServiceAccess::QUERY_STATUS)
            .context("creating the service")?,
    };
    service.set_description(DESCRIPTION)?;
    service.update_failure_actions(ServiceFailureActions {
        reset_period: ServiceFailureResetPeriod::After(Duration::from_secs(24 * 3600)),
        reboot_msg: None,
        command: None,
        actions: Some(vec![ServiceAction { action_type: ServiceActionType::Restart, delay: Duration::from_secs(5) }; 3]),
    })?;
    service.set_failure_actions_on_non_crash_failures(true)?;
    service.start(&[] as &[&OsStr]).context("starting the service")?;

    println!("Installed: {} (service \"{DISPLAY_NAME}\"), server {server}.", exe.display());
    println!("Logs: {}", super::state_dir().join("logs").display());
    Ok(())
}

pub fn uninstall(purge: bool) -> Result<()> {
    let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)
        .context("run as administrator")?;
    match manager.open_service(SERVICE_NAME, ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::DELETE) {
        Ok(service) => {
            stop_and_wait(&service)?;
            service.delete().context("removing the service")?;
            println!("The SMP Agent service was removed.");
        }
        Err(_) => println!("The SMP Agent service isn't installed."),
    }
    super::tray_visibility::remove_windows10_policy();
    if purge {
        let dir = super::state_dir();
        if dir.exists() {
            std::fs::remove_dir_all(&dir).with_context(|| format!("deleting {}", dir.display()))?;
            println!("Deleted {} (identity and logs). Remove the device in SMP too.", dir.display());
        }
    }
    println!("You can delete {} now.", program_dir().display());
    Ok(())
}

/// Copy this exe to C:\Program Files\SMP (only administrators can change files
/// there, which matters: the service runs it as SYSTEM).
fn install_exe() -> Result<PathBuf> {
    let current = std::env::current_exe()?;
    let dir = program_dir();
    let target = dir.join("smp-agent.exe");
    std::fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
    let same = std::fs::canonicalize(&current).ok() == std::fs::canonicalize(&target).ok();
    if !same {
        std::fs::copy(&current, &target).with_context(|| format!("copying to {}", target.display()))?;
    }
    Ok(target)
}

fn program_dir() -> PathBuf {
    let base = std::env::var_os("ProgramFiles").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\Program Files"));
    base.join("SMP")
}

fn stop_and_wait(service: &windows_service::service::Service) -> Result<()> {
    let status = service.query_status()?;
    if status.current_state == ServiceState::Stopped {
        return Ok(());
    }
    let _ = service.stop();
    let deadline = Instant::now() + Duration::from_secs(30);
    while Instant::now() < deadline {
        if service.query_status()?.current_state == ServiceState::Stopped {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    bail!("the service didn't stop within 30 seconds")
}

fn ask(question: &str) -> Result<String> {
    print!("{question}");
    std::io::stdout().flush()?;
    let mut answer = String::new();
    std::io::stdin().read_line(&mut answer)?;
    Ok(answer.trim().to_string())
}

/// "192.168.1.10" -> "https://192.168.1.10"; only https is allowed.
fn normalize_server(input: &str) -> Result<String> {
    let s = input.trim().trim_end_matches('/');
    if s.is_empty() {
        bail!("no server address given");
    }
    if s.starts_with("http://") {
        bail!("SMP is only reachable over https");
    }
    Ok(if s.starts_with("https://") { s.to_string() } else { format!("https://{s}") })
}

// ------------------------------------------------------------------ running as a service

/// `smp-agent service`: called by Windows (never by hand).
pub fn run() -> Result<()> {
    service_dispatcher::start(SERVICE_NAME, ffi_service_main).context("this command is for Windows' service manager only")
}

define_windows_service!(ffi_service_main, service_main);

fn service_main(_arguments: Vec<OsString>) {
    if let Err(e) = run_service() {
        log::error!("service: {e:#}");
    }
}

fn run_service() -> Result<()> {
    let (stop_requested, stop_received) = mpsc::channel();
    let status = service_control_handler::register(SERVICE_NAME, move |control| match control {
        ServiceControl::Stop | ServiceControl::Shutdown => {
            STOP.store(true, Ordering::SeqCst);
            let _ = stop_requested.send(());
            ServiceControlHandlerResult::NoError
        }
        ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
        _ => ServiceControlHandlerResult::NotImplemented,
    })?;
    report(&status, ServiceState::Running, 0)?;
    log::info!("service started");
    super::tray_visibility::ensure_windows10_policy(); // the tray icon must stay visible

    let agent = std::thread::spawn(|| crate::run_agent(&state::default_path(), &STOP));
    // Until Windows asks us to stop, or the agent ends by itself (an error).
    while !agent.is_finished() && stop_received.recv_timeout(Duration::from_millis(500)).is_err() {}
    STOP.store(true, Ordering::SeqCst);
    report(&status, ServiceState::StopPending, 0)?;

    // The agent may be inside a long-poll; give it a few seconds, then just exit
    // (the process ending closes the pipe, so the helper and its icon go too).
    let deadline = Instant::now() + Duration::from_secs(5);
    while !agent.is_finished() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(100));
    }
    let exit_code = match agent.is_finished().then(|| agent.join()) {
        Some(Ok(Err(e))) => {
            log::error!("agent: {e:#}");
            1
        }
        _ => 0,
    };
    log::info!("service stopped");
    report(&status, ServiceState::Stopped, exit_code)?;
    Ok(())
}

fn report(handle: &ServiceStatusHandle, state: ServiceState, exit_code: u32) -> Result<()> {
    handle.set_service_status(ServiceStatus {
        service_type: ServiceType::OWN_PROCESS,
        current_state: state,
        controls_accepted: if state == ServiceState::Running {
            ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN
        } else {
            ServiceControlAccept::empty()
        },
        exit_code: ServiceExitCode::Win32(exit_code),
        checkpoint: 0,
        wait_hint: Duration::from_secs(10),
        process_id: None,
    })?;
    Ok(())
}
