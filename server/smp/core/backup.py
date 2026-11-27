# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)  # ruff: ignore[missing-copyright-notice]
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

r"""Encrypted database backups: `smp backup encrypt|decrypt|verify` (stdin -> stdout),
used by upgrade.sh (tools/db-backup.sh) around `pg_dump` / `pg_restore`.

The key is SMP_BACKUP_KEY, a random secret of its own (install.sh puts it in .env). Not
SMP_SECRET_KEY: a dump holds the encrypted one-time passwords, and that key decrypts
them. Not the database password either: changing that would lock every old backup.

Format "SMPBAK1", streamed so a large database never has to fit in memory (the STREAM
construction, as in `age`):

    header  = b"SMPBAK1\\n" | salt (16) | nonce prefix (7) | key check (16)
    keys    = HKDF-SHA256(SMP_BACKUP_KEY, salt): 32 bytes for AES-256-GCM, 16 for the check
    chunk i = AES-256-GCM(nonce = prefix | i (4 bytes, big endian) | 1 if last else 0,
                          up to 64 KiB of the dump, associated data = header)

Every chunk is authenticated, its position by the counter and the end by the "last"
flag, so a changed, reordered, cut off or extended file is refused. The key check only
tells "wrong key" apart from "damaged file". Decrypting writes each chunk once it is
verified: a cut-off file fails at its end, after the earlier chunks went out, so
restores run `verify` first (tools/db-backup.sh does).
"""  # ruff: ignore[line-too-long, missing-blank-line-after-summary]

from __future__ import annotations

import hmac
import os
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"SMPBAK1\n"
SALT_LEN, PREFIX_LEN, CHECK_LEN, TAG_LEN = 16, 7, 16, 16
HEADER_LEN = len(MAGIC) + SALT_LEN + PREFIX_LEN + CHECK_LEN
CHUNK = 64 * 1024
MAX_CHUNKS = 2**32  # the counter's range: 256 TiB


class BackupError(Exception):
    """A backup that can't be read with this key; the message says why."""


def _keys(secret: str, salt: bytes) -> tuple[AESGCM, bytes]:
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=32 + CHECK_LEN,
        salt=salt,
        info=b"smp database backup v1"
    ).derive(secret.encode())

    return AESGCM(material[:32]), material[32:]


def _nonce(prefix: bytes, i: int, last: bool) -> bytes:  # ruff: ignore[boolean-type-hint-positional-argument]
    if i >= MAX_CHUNKS:
        msg = "the backup is too large for this format"
        raise BackupError(msg)

    return prefix + i.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


def _read(src: BinaryIO, n: int) -> bytes:
    """Up to n bytes; fewer only at the end of the stream (pipes return short reads)."""  # ruff: ignore[docstring-missing-returns, line-too-long]
    parts: list[bytes] = []
    missing = n

    while missing:
        part = src.read(missing)
        if not part:
            break

        parts.append(part)
        missing -= len(part)

    return b"".join(parts)


def encrypt(src: BinaryIO, dst: BinaryIO, secret: str) -> None:  # ruff: ignore[undocumented-public-function]
    salt, prefix = os.urandom(SALT_LEN), os.urandom(PREFIX_LEN)
    aead, check = _keys(secret, salt)
    header = MAGIC + salt + prefix + check
    dst.write(header)
    i, chunk = 0, _read(src, CHUNK)

    while True:
        following = _read(src, CHUNK) if len(chunk) == CHUNK else b""
        last = not following
        dst.write(aead.encrypt(_nonce(prefix, i, last), chunk, header))

        if last:
            return

        i, chunk = i + 1, following


def decrypt(src: BinaryIO, dst: BinaryIO | None, secret: str) -> int:
    """Write the dump to dst (None: only checks it) and returns its size in bytes.

    Returns:
        int: The length of decrypted data in bytes

    Raises:
        BackupError: Unsuccessful decription.

    """  # ruff: ignore[line-too-long]
    header = _read(src, HEADER_LEN)
    if len(header) < HEADER_LEN or not header.startswith(MAGIC):
        msg = "this is not an SMP database backup (or one in a newer format)"
        raise BackupError(msg)
    salt = header[len(MAGIC) : len(MAGIC) + SALT_LEN]
    prefix = header[len(MAGIC) + SALT_LEN : len(MAGIC) + SALT_LEN + PREFIX_LEN]
    aead, check = _keys(secret, salt)
    if not hmac.compare_digest(check, header[-CHECK_LEN:]):
        msg = "wrong SMP_BACKUP_KEY: this backup was made with another key"
        raise BackupError(msg)
    size, i, sealed = 0, 0, _read(src, CHUNK + TAG_LEN)
    while True:
        if len(sealed) < TAG_LEN:
            msg = "the backup is cut off"
            raise BackupError(msg)

        following = _read(src, CHUNK + TAG_LEN) if len(sealed) == CHUNK + TAG_LEN else b""  # ruff: ignore[line-too-long]
        last = not following

        try:
            plain = aead.decrypt(_nonce(prefix, i, last), sealed, header)
        except InvalidTag:
            msg = f"the backup is damaged or cut off (at {size} bytes of the dump)"  # ruff: ignore[line-too-long]
            raise BackupError(msg) from None

        size += len(plain)
        if dst is not None:
            dst.write(plain)
        if last:
            return size

        i, sealed = i + 1, following
