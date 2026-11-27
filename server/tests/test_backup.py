# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Encrypted database backups (smp/core/backup.py, `smp backup`). No database needed."""

import io
import os
import subprocess
import sys

import pytest

from smp.core import backup
from smp.core.backup import CHUNK, HEADER_LEN, TAG_LEN, BackupError, decrypt, encrypt
from smp.core.config import Settings

KEY = "kX9-backup-key-for-tests-0123456789abcdef"


def seal(data: bytes, key: str = KEY) -> bytes:
    out = io.BytesIO()
    encrypt(io.BytesIO(data), out, key)
    return out.getvalue()


def unseal(blob: bytes, key: str = KEY) -> bytes:
    out = io.BytesIO()
    assert decrypt(io.BytesIO(blob), out, key) == len(out.getvalue())
    return out.getvalue()


class Trickle(io.RawIOBase):
    """A pipe that hands out at most 1000 bytes per read."""

    def __init__(self, data: bytes):
        self._data = io.BytesIO(data)

    def readable(self):
        return True

    def read(self, n=-1):
        return self._data.read(min(n if n >= 0 else 1000, 1000))


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 3 * CHUNK + 17])
def test_round_trip(size):
    data = os.urandom(size)
    blob = seal(data)
    chunks = max(1, -(-size // CHUNK))  # an empty dump is one empty chunk
    assert len(blob) == HEADER_LEN + size + TAG_LEN * chunks
    assert unseal(blob) == data


def test_short_reads_from_pipes():
    data = os.urandom(2 * CHUNK + 5)
    out = io.BytesIO()
    encrypt(Trickle(data), out, KEY)
    back = io.BytesIO()
    decrypt(Trickle(out.getvalue()), back, KEY)
    assert back.getvalue() == data


def test_every_backup_is_different():
    assert seal(b"same dump") != seal(b"same dump")  # fresh salt and nonces


def test_wrong_key_is_named():
    with pytest.raises(BackupError, match="wrong SMP_BACKUP_KEY"):
        unseal(seal(b"dump"), KEY + "x")


def test_not_a_backup():
    with pytest.raises(BackupError, match="not an SMP database backup"):
        unseal(b"PGDMP plain dump, not encrypted")
    with pytest.raises(BackupError, match="not an SMP database backup"):
        unseal(b"")


def test_any_changed_byte_is_refused():
    blob = bytearray(seal(os.urandom(2 * CHUNK)))
    for pos in (HEADER_LEN + 5, HEADER_LEN + CHUNK + TAG_LEN + 3, len(blob) - 1, len(backup.MAGIC) + 2):
        bad = bytearray(blob)
        bad[pos] ^= 1
        with pytest.raises(BackupError):
            unseal(bytes(bad))


def test_cut_off_backups_are_refused():
    blob = seal(os.urandom(2 * CHUNK + 100))
    first_chunk_end = HEADER_LEN + CHUNK + TAG_LEN
    for cut in (first_chunk_end, first_chunk_end + CHUNK + TAG_LEN, len(blob) - 1, HEADER_LEN + 3, HEADER_LEN):
        with pytest.raises(BackupError, match="cut off"):
            unseal(blob[:cut])


def test_extended_backups_are_refused():
    blob = seal(os.urandom(CHUNK + 10))
    with pytest.raises(BackupError, match="damaged"):
        unseal(blob + seal(b"more")[HEADER_LEN:])


def test_reordered_chunks_are_refused():
    blob = seal(os.urandom(3 * CHUNK))
    c = CHUNK + TAG_LEN
    head, a, b, rest = blob[:HEADER_LEN], blob[HEADER_LEN : HEADER_LEN + c], blob[HEADER_LEN + c : HEADER_LEN + 2 * c], blob[HEADER_LEN + 2 * c :]
    with pytest.raises(BackupError, match="damaged"):
        unseal(head + b + a + rest)


def test_chunks_cant_be_moved_between_backups():
    one, two = seal(os.urandom(2 * CHUNK)), seal(os.urandom(2 * CHUNK))
    with pytest.raises(BackupError):
        unseal(one[:HEADER_LEN] + two[HEADER_LEN:])


@pytest.mark.parametrize(
    ("key", "problem"),
    [("", "not set"), ("short", "at least 32"), ("a" * 40, "placeholder"), (KEY, None)],
)
def test_backup_key_problems(key, problem):
    found = Settings(backup_key=key).backup_key_problem()
    assert (found is None) if problem is None else (problem in found)


def run_cli(action: str, data: bytes, key: str | None = KEY) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SMP_BACKUP_KEY")}
    if key is not None:
        env["SMP_BACKUP_KEY"] = key
    return subprocess.run([sys.executable, "-m", "smp", "backup", action], input=data, capture_output=True, env=env)


def test_cli_round_trip_and_verify():
    dump = os.urandom(CHUNK + 123)
    sealed = run_cli("encrypt", dump)
    assert sealed.returncode == 0, sealed.stderr
    assert run_cli("decrypt", sealed.stdout).stdout == dump
    ok = run_cli("verify", sealed.stdout)
    assert ok.returncode == 0 and f"{len(dump)} bytes".encode() in ok.stdout


def test_cli_explains_failures():
    sealed = run_cli("encrypt", b"dump").stdout
    assert b"wrong SMP_BACKUP_KEY" in run_cli("verify", sealed, KEY + "x").stderr
    assert b"cut off" in run_cli("verify", sealed[:-1]).stderr
    missing = run_cli("encrypt", b"dump", key=None)
    assert missing.returncode != 0 and b"SMP_BACKUP_KEY" in missing.stderr
