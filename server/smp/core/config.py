import os
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Process-wide configuration, read from environment variables prefixed with SMP_.

    Per-school (per-org) configuration does NOT belong here; it lives in the
    module_settings table so it can be edited from the portal.
    """

    model_config = SettingsConfigDict(env_prefix="SMP_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://smp:smp@localhost:5432/smp"
    db_echo: bool = False

    # Slug of the org used when a login request does not name one.
    # Single-school installs never need to think about orgs.
    default_org: str = "default"

    session_ttl_hours: int = 12
    session_cookie_name: str = "smp_session"
    # Set to false only for local development over plain http.
    cookie_secure: bool = True

    # Login throttling (in memory, per server process; see smp/core/throttle.py).
    # Failed logins are counted per account, from any IP; unknown usernames are
    # throttled exactly like real ones (no enumeration oracle).
    login_max_failures: int = 5  # failed logins for one username ...
    login_lockout_seconds: int = 60  # ... lock it for this long
    login_ip_attempts_per_minute: int = 20  # all login attempts from one IP

    # Device agents: how long a heartbeat may wait for a config change (long-poll).
    # Below nginx's proxy_read_timeout; agents call again right away.
    agent_longpoll_seconds: float = 25.0
    # Folder with the Windows agent (smp-agent.exe + smp-agent.version) offered at
    # /downloads/smp-agent.exe. The Docker image builds it and sets this.
    agent_download_dir: str | None = None

    # Reverse proxies whose X-Forwarded-For is trusted (comma separated IPs/CIDRs, "*" = any).
    # Must include the proxy, or every client looks like the proxy's IP to the throttle.
    forwarded_allow_ips: str = "127.0.0.1"

    # How many argon2 hashes may run at once (each uses up to 64 MiB). 0 = half the CPUs.
    hash_concurrency: int = 0

    # Server secret: encrypts one-time passwords at rest so admins can export login
    # slips until they're used. Required: the server refuses to start without it.
    # Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"
    # Prefer SMP_SECRET_KEY_FILE (e.g. a Docker secret, chmod 600) over the env var,
    # and keep it out of database backups: key + DB dump = readable unused OTPs.
    secret_key: str = ""
    secret_key_file: str = ""
    # Key rotation: put the old key(s) here (comma separated) while switching to a new
    # SMP_SECRET_KEY, run `smp reencrypt-otps`, then remove them.
    secret_key_previous: str = ""

    @model_validator(mode="after")
    def _read_key_file(self):
        if self.secret_key_file and not self.secret_key:
            self.secret_key = Path(self.secret_key_file).read_text().strip()
        return self

    def previous_keys(self) -> list[str]:
        return [k.strip() for k in self.secret_key_previous.split(",") if k.strip()]

    def secret_key_problem(self) -> str | None:
        """Why the configured secret key is unusable, or None if it's fine."""
        if not self.secret_key:
            return "SMP_SECRET_KEY (or SMP_SECRET_KEY_FILE) is not set"
        if len(self.secret_key) < MIN_SECRET_KEY_LENGTH:
            return f"SMP_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters"
        if len(set(self.secret_key)) < 10:
            return "SMP_SECRET_KEY looks like a placeholder (too few distinct characters); generate a random one"
        return None

    def effective_hash_concurrency(self) -> int:
        return self.hash_concurrency or max(1, (os.cpu_count() or 2) // 2)

    # Comma separated module names; empty means "all registered modules".
    enabled_modules: str = ""

    # Allowed browser origins for CORS (comma separated). Empty = same-origin only.
    cors_origins: str = ""

    # Directory with the built web UI (WEBUI/dist). When set, the server serves it at /
    # so UI and API share one origin. Empty = API only (use the Vite dev server).
    ui_dir: str = ""

    # DNS sinkhole integration. These are process-level deployment settings, not
    # per-school state. Mutations are synchronized from the control plane to the
    # Go CoreDNS service after the database transaction has committed.
    dns_sinkhole_url: str = "http://127.0.0.1:8080"
    dns_sync_token: str = ""

    def enabled_module_names(self) -> set[str] | None:
        names = {n.strip() for n in self.enabled_modules.split(",") if n.strip()}
        return names or None


@lru_cache
def get_settings() -> Settings:
    return Settings()
