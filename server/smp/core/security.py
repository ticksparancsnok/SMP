import asyncio
import base64
import hashlib
import logging
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from smp.core.config import get_settings

# ---------------------------------------------------------------------------- passwords

_hasher = PasswordHasher()  # argon2id, 64 MiB, for passwords people choose
# One-time passwords are 12 random characters (~59 bits): no hash setting changes the
# fact that they can't be brute-forced, so they get lighter parameters than
# human-chosen passwords to keep bulk imports fast (~7 ms each, ~7 s per 1000
# students). argon2 hashes carry their parameters, so verify_password() handles both.
_otp_hasher = PasswordHasher(time_cost=1, memory_cost=16 * 1024, parallelism=1)
# TODO(evaluate, issue #4): timing side channel. A failed login against an account that still
# has its one-time password takes ~16 ms (this cheap hash), against an unknown
# username ~47 ms (_DUMMY_HASH), so response times tell "exists and not activated
# yet" apart from "doesn't exist". Not exploitable for guessing (OTPs are ~59 bits)
# and SMP normally runs on the school's own network; if it's ever exposed publicly,
# make every failed login cost the same, e.g. also verify _DUMMY_HASH when the
# stored hash is an OTP hash (see login() in core/api/auth.py).
# Used to spend the same time on unknown usernames as on wrong passwords.
_DUMMY_HASH = _hasher.hash("smp-dummy-password")

log = logging.getLogger(__name__)

# argon2 is deliberately CPU- and memory-heavy. Run it off the event loop (otherwise
# every login freezes the whole server) and cap how many run at once (otherwise a
# login flood exhausts RAM: 64 MiB each). Size: SMP_HASH_CONCURRENCY.
_hash_slots: asyncio.Semaphore | None = None


def _slots() -> asyncio.Semaphore:
    global _hash_slots
    if _hash_slots is None:
        _hash_slots = asyncio.Semaphore(get_settings().effective_hash_concurrency())
    return _hash_slots


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


async def hash_password_async(password: str) -> str:
    async with _slots():
        return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password_hash: str | None, password: str) -> bool:
    async with _slots():
        return await asyncio.to_thread(verify_password, password_hash, password)


# ---------------------------------------------------------------------------- tokens


def hash_token(token: str) -> str:
    """Tokens are high-entropy random strings, so a plain SHA-256 is sufficient."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> tuple[str, str]:
    """Return (plaintext, sha256). Only the hash is ever stored."""
    token = f"{prefix}_{secrets.token_urlsafe(32)}"
    return token, hash_token(token)


# ---------------------------------------------------------------------------- one-time passwords

# No 0/o, 1/l/i: these get copied from a printed slip by children.
OTP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_otp() -> str:
    """e.g. "k7mp-x4qr-9zte" (3 groups of 4, ~59 bits)."""
    return "-".join("".join(secrets.choice(OTP_ALPHABET) for _ in range(4)) for _ in range(3))


def hash_otps(otps: list[str]) -> list[str]:
    return [_otp_hasher.hash(o) for o in otps]


async def hash_otps_async(otps: list[str]) -> list[str]:
    async with _slots():
        return await asyncio.to_thread(hash_otps, otps)


class SecretKeyMissing(RuntimeError):
    pass


def _derive(key: str) -> Fernet:
    # Domain-separated derivation, so the same secret can key other things later.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"smp-otp-v1:" + key.encode()).digest()))


def _fernet() -> MultiFernet:
    """Encrypts with SMP_SECRET_KEY; also decrypts with SMP_SECRET_KEY_PREVIOUS so a
    key rotation doesn't make unused OTPs unreadable (see `smp reencrypt-otps`)."""
    settings = get_settings()
    if problem := settings.secret_key_problem():
        raise SecretKeyMissing(problem)
    return MultiFernet([_derive(settings.secret_key), *(_derive(k) for k in settings.previous_keys())])


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str | None:
    """None if no configured key can decrypt it (the key was replaced without keeping
    the old one in SMP_SECRET_KEY_PREVIOUS). Logged, so admins can find out why an
    OTP is missing from the login slips; the OTP itself still works for login."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        log.warning("a stored one-time password could not be decrypted with the configured SMP_SECRET_KEY(s)")
        return None


def reencrypt_secret(token: str) -> str | None:
    """Re-encrypt with the current key (after a rotation); None if unreadable."""
    try:
        return _fernet().rotate(token.encode()).decode()
    except InvalidToken:
        return None


def require_secret_key() -> None:
    """Fail early (before writing anything) when OTPs can't be stored."""
    _fernet()
