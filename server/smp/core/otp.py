# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""One-time passwords (OTPs): the initial password of an account someone else set up.

Each user gets their own random OTP (never a shared one), valid for
`core.otp_valid_days`. The OTP *is* the account password until first login (its
argon2 hash is in password_hash), and at first login the user must replace it. An
encrypted copy is kept so admins can export login slips until it's used.
"""

from datetime import datetime, timedelta

from smp.core.context import Ctx
from smp.core.db import utcnow
from smp.core.models import User
from smp.core.security import decrypt_secret, encrypt_secret, generate_otp, hash_otps_async, require_secret_key


async def issue_otps(ctx: Ctx, users: list[User]) -> list[str]:
    """Give each user a fresh OTP (replacing their password). Returns the plaintexts in
    the same order; they're shown once and remain exportable until used/expired."""
    require_secret_key()  # before touching anything
    days = (await ctx.settings("core")).otp_valid_days  # type: ignore[attr-defined]
    expires = utcnow() + timedelta(days=days)
    otps = [generate_otp() for _ in users]
    hashes = await hash_otps_async(otps)
    for user, otp, digest in zip(users, otps, hashes, strict=True):
        set_otp(user, otp, digest, expires)
    return otps


def set_otp(user: User, otp: str, digest: str, expires: datetime) -> None:
    """Make `otp` (hashed as `digest`) the user's password until `expires`; they must
    choose their own at first login. Shared by the API and `smp bootstrap --otp`."""
    user.password_hash = digest
    user.otp_secret = encrypt_secret(otp)
    user.otp_expires_at = expires
    user.must_change_password = True


def clear_otp(user: User) -> None:
    user.otp_secret = None
    user.otp_expires_at = None


def otp_state(user: User, now: datetime | None = None) -> str | None:
    """'active' | 'expired' | None (no OTP)."""
    if user.otp_expires_at is None:
        return None
    return "active" if user.otp_expires_at > (now or utcnow()) else "expired"


def readable_otp(user: User, now: datetime | None = None) -> str | None:
    """The plaintext OTP if it's still usable (for login slips), else None."""
    if user.otp_secret is None or otp_state(user, now) != "active":
        return None
    return decrypt_secret(user.otp_secret)
