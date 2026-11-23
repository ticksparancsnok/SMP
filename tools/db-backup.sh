# shellcheck shell=bash
# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

# Encrypted backups of SMP's database; sourced (bash) by upgrade.sh, run from the
# install folder (docker compose finds the stack there).
#
#   smp_db_backup FILE    pg_dump of the running database, encrypted -> FILE (chmod 600),
#                         then checked by smp_db_verify; FILE appears only if all went well
#   smp_db_verify FILE    decrypts FILE completely and checks that pg_restore can read it
#   smp_db_restore FILE   replaces the database with FILE. The server must be stopped.
#                         The replaced database is kept as smp_before_restore (until the
#                         next restore) in case the restore itself was the mistake.
#   smp_db_revision       the database's migration revision (Alembic), e.g. to tell whether
#                         a failed upgrade had already changed the database's layout
#
# Encryption is `smp backup` (server/smp/core/backup.py: streamed AES-256-GCM) with
# SMP_BACKUP_KEY from .env, in a one-off server container: the key reaches only that
# container (not the running server, not a command line), and the host needs nothing
# but docker. The dump itself never touches the disk unencrypted.
#
# TODO(scheduled-backups, issue #13): backups are only made by upgrade.sh, before an
# upgrade. smp_db_backup is meant to be reused for regular (e.g. nightly) backups, with
# their own retention (upgrade.sh keeps only its newest backup) and a copy off the server.

# The server image's `smp backup`; works whether or not the server is running. Compose's
# own progress lines are shown only when it fails (they'd bury the messages that matter).
_smp_backup_cmd() {
    local err status=0
    err=$(mktemp)
    docker compose run --rm --no-deps -T -e SMP_BACKUP_KEY server smp backup "$1" 2>"$err" || status=$?
    [ $status = 0 ] || grep -v -E '^ *Container .* (Creating|Created)' "$err" >&2
    rm -f "$err"
    return $status
}

_smp_db_up() {
    docker compose up -d --wait db </dev/null >/dev/null 2>&1 || { echo "the database container doesn't start" >&2; return 1; }
}

smp_db_backup() {
    local out=$1
    _smp_db_up || return 1
    (
        set -o pipefail
        umask 077
        docker compose exec -T db pg_dump -U smp -d smp --format=custom </dev/null | _smp_backup_cmd encrypt >"$out.part"
    ) || { rm -f "$out.part"; echo "the database backup failed" >&2; return 1; }
    smp_db_verify "$out.part" >/dev/null || { rm -f "$out.part"; echo "the new backup doesn't read back; not kept" >&2; return 1; }
    chmod 600 "$out.part"
    mv -f "$out.part" "$out"
}

smp_db_revision() {
    _smp_db_up || return 1
    docker compose exec -T db psql -U smp -d smp -tAq -c \
        "SELECT coalesce(string_agg(version_num, ',' ORDER BY version_num), '') FROM alembic_version" </dev/null
}

smp_db_verify() {
    local file=$1
    [ -s "$file" ] || { echo "no backup at $file" >&2; return 1; }
    _smp_db_up || return 1
    # The whole file is decrypted (a damaged or cut-off one fails), and pg_restore reads
    # the dump's table of contents; `cat` takes the rest so nothing stops early.
    (
        set -o pipefail
        _smp_backup_cmd decrypt <"$file" | docker compose exec -T db sh -c 'pg_restore --list >/dev/null && cat >/dev/null'
    ) || { echo "the backup $file can't be read (wrong SMP_BACKUP_KEY in .env, or a damaged file)" >&2; return 1; }
}

smp_db_restore() {
    local file=$1 psql=(docker compose exec -T -e PGOPTIONS=--client-min-messages=warning db psql -U smp -d postgres -v ON_ERROR_STOP=1 -q)
    smp_db_verify "$file" || return 1  # never touch the database for a backup that doesn't open
    "${psql[@]}" -c 'DROP DATABASE IF EXISTS smp_restore' -c 'CREATE DATABASE smp_restore OWNER smp' </dev/null \
        || { echo "can't create a database to restore into" >&2; return 1; }
    # Into a new database first: until it has fully worked, the current one stays as it is.
    if ! (
        set -o pipefail
        _smp_backup_cmd decrypt <"$file" | docker compose exec -T db pg_restore -U smp -d smp_restore --exit-on-error --single-transaction
    ); then
        "${psql[@]}" -c 'DROP DATABASE IF EXISTS smp_restore' </dev/null || true
        echo "restoring the backup failed; the database is unchanged" >&2
        return 1
    fi
    "${psql[@]}" -c 'DROP DATABASE IF EXISTS smp_before_restore WITH (FORCE)' \
        -c 'ALTER DATABASE smp RENAME TO smp_before_restore' \
        -c 'ALTER DATABASE smp_restore RENAME TO smp' </dev/null \
        || { echo "couldn't switch to the restored database (is the server still connected?)" >&2; return 1; }
}
