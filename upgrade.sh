#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

# SMP upgrader: run it in the folder where SMP is installed to download and apply the
# newest version, or to go back to the version before the last upgrade.
#
#   ./upgrade.sh                 newest version (the main branch on GitHub)
#   ./upgrade.sh --rollback      back to before the last upgrade (files and database)
#   ./upgrade.sh --force-components   also a new build of the same release: upgrades when any
#                                component's version in Version.toml differs, not only [release]
#   ./upgrade.sh --help          options: another branch/tag, your own download, ...
#
# 1. checks the installation (made by install.sh; not a git checkout: update those with git)
# 2. downloads SMP (the same way as install.sh: --ref, or --download-url for a modified
#    SMP on your own server) and compares its [release] version in Version.toml with the
#    installed one: nothing to do if it isn't newer (--force reinstalls / downgrades)
# 3. shows what changes: each component's version, and the files added, changed and
#    removed (files someone edited here are pointed out), then asks
# 4. backs up, into backups/<time>-from-<version>/:
#      database.smpbackup  the whole database, encrypted with SMP_BACKUP_KEY from .env
#                          (tools/db-backup.sh); checked by reading it back
#      the files it replaces or removes, and lists of what it adds/changes/removes
# 5. replaces the files. Never touched: .env, nginx/certs/ (the PC agents trust exactly
#    that certificate), the Docker volumes (database, DNS lists), backups/
#    (plus installed-version and db-revision: what was running, and the database's
#    migration revision, before the upgrade)
# 6. rebuilds and restarts with the new version's ./install.sh --local, which keeps .env,
#    migrates the database and prints no usernames or passwords. The images are built
#    while the old SMP keeps running; only then are the containers swapped (seconds), so
#    the school's DNS and the PCs' connections are interrupted only briefly.
#    If this fails, it goes back by itself: the old files, the old version built and
#    started again, and the database imported back from the backup if the new version
#    had already changed its layout (otherwise the database is kept: nothing is lost).
#    If replacing the files fails, only the files are put back (nothing else had changed).
# 7. summarizes: versions before -> after, files changed, where the backup is, and removes
#    the older upgrade backups: only the newest one is kept
#
# --rollback [backups/<dir>] undoes an upgrade later: puts the backed-up files back, builds
# the old version, then (server stopped) imports the database from the backup, so
# everything done in SMP since that upgrade is lost. The database it replaces is kept as
# smp_before_restore until the next restore.
#
# PCs update their agent by themselves to the one the new version offers (within a few
# minutes, see agent/README.md "Updates"; Settings -> Device agents can switch it off).
set -euo pipefail

if [ -t 1 ]; then B=$'\e[1m' R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' N=$'\e[0m'; else B="" R="" G="" Y="" N=""; fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
warn() { printf '%s%s%s\n' "$Y" "$*" "$N"; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
yesno() {  # yesno "question" y|n   -> exit status 0 for yes
    local reply="" hint="y/N"
    [ "$2" = y ] && hint="Y/n"
    read -r -p "$1 [$hint]: " reply || true
    reply=${reply:-$2}
    [[ $reply =~ ^[Yy] ]]
}
env_get() { sed -n "s/^$1=//p" .env | tail -n 1; }  # never `source` .env
show_list() {  # show_list FILE TITLE: up to 25 paths, then a count
    local n
    n=$(wc -l <"$1")
    [ "$n" -gt 0 ] || return 0
    say "$2 ($n):"
    head -n 25 "$1" | sed 's/^/    /'
    [ "$n" -le 25 ] || say "    ... and $((n - 25)) more"
}
versions_table() {  # versions_table OLD_VERSION_TOML NEW_VERSION_TOML
    local s old new
    for s in release server WEBUI agent Site-blocker; do
        old=$(sh tools/version.sh "$s" "$1" 2>/dev/null || echo "-")
        new=$(sh tools/version.sh "$s" "$2" 2>/dev/null || echo "-")
        if [ "$old" = "$new" ]; then
            printf '  %-13s %s\n' "$s" "$old"
        else
            printf '  %-13s %s -> %s%s%s\n' "$s" "$old" "$B" "$new" "$N"
        fi
    done
}
component_changes() {  # component_changes "section<TAB>old<TAB>new<TAB>up|down" lines (tools/version.sh --diff)
    local section old new way
    while IFS="$(printf '\t')" read -r section old new way; do
        [ -n "$section" ] || continue
        printf '  %-13s %s -> %s%s%s%s\n' "$section" "$old" "$B" "$new" "$N" "$([ "$way" = down ] && echo "   (older)")"
    done <<<"$1"
}
usage() {
    cat <<EOF
Usage: ./upgrade.sh [options]
       ./upgrade.sh --rollback [backups/<dir>]

Downloads the newest SMP, shows what changes, backs up the database (encrypted) and the
files it replaces into backups/, replaces the files and rebuilds. .env, the HTTPS
certificate and all data are kept.

  --ref REF            a branch or tag on GitHub (default: main)
  --download-url URL   download this .tar.gz of SMP instead (e.g. a modified SMP)
  --sha256 HASH        refuse the download unless its SHA-256 is HASH
  --yes                don't ask before changing anything
  --force              also when the download isn't newer (reinstall, or downgrade)
  --force-components   also when [release] is the same, if any component's version
                       ([server], [WEBUI], [agent], [Site-blocker]) differs: e.g. a
                       development build (1.0.3 B -> 1.0.3 C) of the same release
  --no-network-check   skip checking that the build can reach the package registries
                       (PyPI, npm, Rust crates, Go modules) before changing anything
  --no-db-backup       skip the database backup (e.g. you make your own); a rollback
                       can then only put the files back
  --rollback [DIR]     go back to before an upgrade: its files and its database backup
                       (default: the newest backup not rolled back yet). Everything done
                       in SMP since that upgrade is lost

A failed upgrade goes back to the previous version by itself. After a successful one,
only the newest backup in backups/ is kept.
  -h, --help           this help
EOF
}

# Everything runs from main(): bash has read the whole function before step 5 replaces
# this file (which is also written next to the old one and renamed, never overwritten).
main() {
    local fetch_args=() assume_yes=no force=no components=no db_backup=yes rollback=no rollback_dir="" net_check=yes
    while [ $# -gt 0 ]; do
        case $1 in
            --ref | --download-url | --sha256)
                [ $# -ge 2 ] && [ -n "$2" ] || die "$1 needs a value (see --help)"
                fetch_args+=("$1" "$2")
                shift
                ;;
            --yes) assume_yes=yes ;;
            --force) force=yes ;;
            --force-components) components=yes ;;
            --no-db-backup) db_backup=no ;;
            --no-network-check) net_check=no ;;
            --rollback)
                rollback=yes
                if [ $# -ge 2 ] && [[ $2 != -* ]]; then rollback_dir=$2; shift; fi
                ;;
            -h | --help) usage; exit 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
        shift
    done
    local here
    here=$(cd "$(dirname "$0")" && pwd)
    cd "$here"

    # ---------------------------------------------------------------- 1. installation
    step "Checking the installation"
    [ ! -e .git ] || die "$here is a git checkout: update it with git (git pull), then run ./install.sh"
    [ -f Version.toml ] && [ -f docker-compose.yml ] && [ -f install.sh ] && [ -f .smp-manifest ] \
        || die "no SMP installation in $here (install it with install.sh first)"
    [ -f .env ] || die "no .env in $here: restore your .env backup first"
    command -v docker >/dev/null || die "docker is not installed"
    docker compose version </dev/null >/dev/null 2>&1 || die "the docker compose plugin is missing"
    docker info </dev/null >/dev/null 2>&1 || die "can't talk to the docker daemon (is it running? are you in the docker group?)"
    if [ $db_backup = yes ] || [ $rollback = yes ]; then
        [ -n "$(env_get SMP_BACKUP_KEY)" ] || die "SMP_BACKUP_KEY is missing in .env (./install.sh --local creates one); or --no-db-backup"
    fi
    local installed
    installed=$(sh tools/version.sh release) || die "can't read the installed version from Version.toml"
    say "installed: SMP $installed in $here"
    # shellcheck source=tools/db-backup.sh
    source tools/db-backup.sh

    if [ $rollback = yes ]; then
        rollback_to "$rollback_dir" "$installed" "$assume_yes" manual
        return
    fi

    # ---------------------------------------------------------------- 2. download
    step "Downloading"
    local work new offered
    work=$(mktemp -d "${TMPDIR:-/tmp}/smp-upgrade.XXXXXX")
    # shellcheck disable=SC2064  # expand now: $work is local to main()
    trap "rm -rf '$work'" EXIT
    bash ./install.sh --fetch "$work/smp" "${fetch_args[@]}" || die "couldn't download SMP (see above)"
    new=$work/smp
    offered=$(sh "$new/tools/version.sh" release "$new/Version.toml")
    case $(sh "$new/tools/version.sh" --compare "$installed" "$offered") in
        lt) say "available: SMP $offered" ;;
        eq)
            # Same release; its components may still have moved on (1.0.3 B -> 1.0.3 C).
            local changed
            # (the installed version.sh: it came with this upgrade.sh, so it knows --diff)
            changed=$(sh tools/version.sh --diff Version.toml "$new/Version.toml") \
                || die "can't compare the component versions (see above)"
            if [ $force = yes ]; then
                say "available: SMP $offered, the installed version (--force: reinstalling it)"
            elif [ -z "$changed" ]; then
                say "${G}SMP $installed is up to date${N} (every component too)."
                exit 0
            elif [ $components = no ]; then
                say "${G}SMP $installed is up to date.${N} Some components differ in the download, though:"
                component_changes "$changed"
                say "./upgrade.sh --force-components installs them."
                exit 0
            else
                say "available: SMP $offered with other component versions (--force-components):"
                component_changes "$changed"
                if printf '%s\n' "$changed" | grep -q "$(printf '\t')down$"; then
                    warn "Some components go BACK to an older version (above): their database changes aren't undone."
                fi
            fi
            ;;
        gt)
            [ $force = yes ] || die "the download is SMP $offered, older than the installed $installed (--force installs it anyway; ./upgrade.sh --rollback goes back with the database)"
            warn "the download is SMP $offered, OLDER than the installed $installed (--force)."
            warn "The database stays at the newer version's layout; if the older server can't use it, it won't start"
            warn "(./upgrade.sh --rollback goes back to the database from before the last upgrade instead)."
            ;;
    esac

    # ---------------------------------------------------------------- 3. what changes
    step "What changes"
    # shellcheck source=tools/release.sh
    source "$new/tools/release.sh"
    smp_plan "$new" "$here" "$work/plan" || die "can't use this download (see above)"
    versions_table Version.toml "$new/Version.toml"
    say ""
    show_list "$work/plan/added" "new files"
    show_list "$work/plan/changed" "changed files"
    show_list "$work/plan/removed" "removed files (not in $offered any more)"
    if [ -s "$work/plan/modified" ]; then
        warn "Changed here by hand since they were installed (the backup keeps your version):"
        sed 's/^/    /' "$work/plan/modified"
    fi
    if [ ! -s "$work/plan/added" ] && [ ! -s "$work/plan/changed" ] && [ ! -s "$work/plan/removed" ]; then
        say "no files differ"
    fi
    local agent_old agent_new
    agent_old=$(sh tools/version.sh agent 2>/dev/null || echo "?")
    agent_new=$(sh tools/version.sh agent "$new/Version.toml")
    if [ "$agent_old" != "$agent_new" ]; then
        say ""
        say "The PC agent changes ($agent_old -> $agent_new): the PCs install it by themselves within a"
        say "few minutes after the upgrade (unless Settings -> Device agents switched that off)."
    fi
    [ $db_backup = yes ] || warn "--no-db-backup: no database backup; a rollback could only put the files back."
    if [ $net_check = yes ] && [ -f "$new/tools/preflight.sh" ]; then  # (older downloads don't have it)
        # The rebuild downloads what changed (Python, npm, Rust, Go packages). Without
        # internet access it would fail halfway and roll back: find out now instead.
        step "Checking that the build can download what it needs"
        # shellcheck source=tools/preflight.sh
        source "$new/tools/preflight.sh"
        if ! smp_check_registries; then
            say "This server (or Docker on it) can't reach the package registries above right now:"
            say "check its DNS (/etc/resolv.conf), firewall or proxy. ./upgrade.sh --no-network-check skips this test."
            die "stopped; nothing was changed"
        fi
        say "package registries reachable"
    fi
    say ""
    if [ $assume_yes = no ]; then
        yesno "Upgrade SMP $installed to $offered now?" y || die "stopped; nothing was changed"
    fi

    # ---------------------------------------------------------------- 4. backup
    local backup cert_before cert_after db_size=""
    backup=backups/$(date +%Y%m%d-%H%M%S)-from-${installed// /}
    mkdir -p -m 700 backups
    mkdir -p "$backup"
    # What runs now, to come back to if the upgrade fails (rollback_to).
    printf '%s\n' "$installed" >"$backup/installed-version"
    smp_db_revision >"$backup/db-revision" 2>/dev/null || rm -f "$backup/db-revision"
    if [ $db_backup = yes ]; then
        step "Backing up the database (encrypted with SMP_BACKUP_KEY)"
        smp_db_backup "$backup/database.smpbackup" || { rm -rf -- "$backup"; die "no backup, so nothing was changed (see above; --no-db-backup skips it)"; }
        db_size=$(du -h "$backup/database.smpbackup" | cut -f1)
        say "$backup/database.smpbackup ($db_size), read back and checked"
    fi

    # ---------------------------------------------------------------- 5. files
    step "Replacing the files"
    cert_before=$(sha256sum nginx/certs/fullchain.pem 2>/dev/null | cut -d' ' -f1 || true)
    cp Version.toml "$work/installed-Version.toml"
    if ! smp_apply "$new" "$here" "$work/plan" "$here/$backup"; then
        warn "Replacing the files failed (see above); putting the previous ones back."
        smp_restore_files "$here/$backup" "$here" || die "putting them back failed too: the previous files are in $backup/ (./upgrade.sh --rollback $backup)"
        date '+%Y-%m-%d %H:%M:%S' >"$backup/ROLLED-BACK"
        die "the upgrade was undone; SMP $installed is unchanged (its containers kept running)"
    fi
    say "$(cat "$work/plan/added" "$work/plan/changed" | wc -l) files written, $(wc -l <"$work/plan/removed") removed; previous copies: $backup/"

    # ---------------------------------------------------------------- 6. rebuild
    step "Rebuilding with SMP $offered's installer"
    if ! bash ./install.sh --local; then
        printf '\n%sSMP %s did not build or start (see above): going back to %s.%s\n' "$R" "$offered" "$installed" "$N"
        if ! (rollback_to "$backup" "$offered" yes auto); then
            printf '\n%sGoing back did not finish either (see above).%s\n' "$R" "$N"
            say "The previous files and the database backup are in $backup/; once the problem is fixed:"
            say "  ./upgrade.sh --rollback $backup"
            exit 1
        fi
        die "the upgrade to SMP $offered failed; SMP $installed runs again (backup: $backup/)"
    fi

    # ---------------------------------------------------------------- 7. summary
    cert_after=$(sha256sum nginx/certs/fullchain.pem 2>/dev/null | cut -d' ' -f1 || true)
    step "Upgraded: SMP $installed -> $offered"
    versions_table "$work/installed-Version.toml" Version.toml
    say ""
    say "files:    $(wc -l <"$work/plan/added") new, $(wc -l <"$work/plan/changed") changed, $(wc -l <"$work/plan/removed") removed"
    say "backup:   $backup/ (${db_size:+database $db_size, }the previous files; ./upgrade.sh --rollback goes back)"
    prune_backups "$backup"
    check_certificate "$cert_before" "$cert_after"
}

# Only the newest upgrade backup is kept: a rollback can only go one upgrade back anyway
# (older backups belong to older files and database layouts). Only folders upgrade.sh made
# are removed; anything else in backups/ stays.
# TODO(scheduled-backups, issue #13): regular backups (tools/db-backup.sh) will need a
# retention of their own; keep them out of this pruning (e.g. backups/scheduled/).
prune_backups() {  # prune_backups KEEP_DIR
    local d removed=0
    for d in backups/*-from-*/; do
        d=${d%/}
        if [ -d "$d" ] && [ "$d" != "$1" ] && [ -f "$d/changed-files.txt" ]; then
            rm -rf -- "$d"
            removed=$((removed + 1))
        fi
    done
    [ $removed = 0 ] || say "          $removed older backup(s) removed: only the newest is kept"
}

check_certificate() {  # check_certificate SHA256_BEFORE SHA256_AFTER
    if [ -n "$1" ] && [ "$1" != "$2" ]; then
        printf '\n%sThe HTTPS certificate changed.%s\n' "$R" "$N"
        say "PC agents trust exactly the certificate they first saw (unless it's a certificate the"
        say "PCs trust anyway), so they now refuse this server. Check nginx/certs/ and SMP_SERVER_NAMES"
        say "in .env; with the old certificate back (docker compose restart nginx) they reconnect."
    fi
}

# rollback_to [DIR] INSTALLED_VERSION ASSUME_YES manual|auto
#   manual (--rollback): always imports the database backup (asks first).
#   auto (a failed upgrade): imports it only if the new version had already changed the
#   database's layout (its migration revision differs from db-revision); otherwise the
#   database is still the old version's and is kept, with everything done meanwhile.
rollback_to() {
    local dir=$1 installed=$2 assume_yes=$3 mode=$4 target_version have_db cert_before
    if [ -z "$dir" ]; then  # the newest one not rolled back yet (names start with the time)
        local d
        for d in backups/*-from-*/; do
            if [ -d "$d" ] && [ ! -e "$d/ROLLED-BACK" ]; then dir=$d; fi
        done
        [ -n "$dir" ] || die "no upgrade backup to go back to in backups/"
    fi
    dir=${dir%/}
    [ -f "$dir/changed-files.txt" ] || die "$dir is not a backup made by upgrade.sh"
    [ ! -e "$dir/ROLLED-BACK" ] || warn "$dir was rolled back to before ($(cat "$dir/ROLLED-BACK")); doing it again."
    target_version=$(cat "$dir/installed-version" 2>/dev/null || sh tools/version.sh release "$dir/Version.toml" 2>/dev/null || sh tools/version.sh release)
    have_db=no
    [ -f "$dir/database.smpbackup" ] && have_db=yes
    local keep_db=no
    if [ "$mode" = auto ] && [ -s "$dir/db-revision" ] && [ "$(smp_db_revision 2>/dev/null)" = "$(cat "$dir/db-revision")" ]; then
        keep_db=yes  # no migration ran: the database is still SMP $target_version's
        have_db=no
    fi

    step "Going back to SMP $target_version (from $installed), using $dir/"
    say "files:     $(wc -l <"$dir/changed-files.txt") put back, $(wc -l <"$dir/removed-files.txt") restored, $(wc -l <"$dir/added-files.txt") removed"
    if [ $keep_db = yes ]; then
        say "database:  kept as it is (the upgrade hadn't changed its layout yet: nothing is lost)"
    elif [ $have_db = yes ]; then
        say "database:  back to the backup of $(date -r "$dir/database.smpbackup" '+%Y-%m-%d %H:%M')"
        warn "Everything done in SMP since then is lost: new or changed users, devices, settings,"
        warn "blocklist entries, audit log entries. (The current database is kept as smp_before_restore.)"
    else
        warn "This backup has no database (--no-db-backup): only the files go back, the database stays as"
        warn "it is now. If the newer version changed its layout, SMP $target_version may not start."
    fi
    if [ "$assume_yes" = no ]; then
        yesno "Go back to SMP $target_version?" n || die "stopped; nothing was changed"
    fi

    if [ $have_db = yes ]; then
        step "Checking the database backup"
        smp_db_verify "$dir/database.smpbackup" || die "nothing was changed"
        say "it opens with this SMP_BACKUP_KEY and holds a complete dump"
    fi

    step "Putting the files back"
    cert_before=$(sha256sum nginx/certs/fullchain.pem 2>/dev/null | cut -d' ' -f1 || true)
    # shellcheck source=tools/release.sh
    source tools/release.sh
    smp_restore_files "$dir" "$PWD" || die "putting the files back failed (see above); run ./upgrade.sh --rollback $dir again"
    say "files of SMP $target_version are back"

    step "Building SMP $target_version (the current one keeps running meanwhile)"
    docker compose build </dev/null || die "building failed (see above); fix it and run ./upgrade.sh --rollback $dir again"

    if [ $have_db = yes ]; then
        step "Restoring the database (SMP is offline until it's back up)"
        docker compose stop server </dev/null
        # shellcheck source=tools/db-backup.sh
        source tools/db-backup.sh  # the old version's copy, now in place
        smp_db_restore "$dir/database.smpbackup" || die "restoring failed (see above); the server is stopped. Fix it and run ./upgrade.sh --rollback $dir again"
        say "database restored"
    fi

    step "Starting SMP $target_version"
    bash ./install.sh --local || die "SMP $target_version did not start (see above)"
    date '+%Y-%m-%d %H:%M:%S' >"$dir/ROLLED-BACK"
    step "Back at SMP $target_version"
    check_certificate "$cert_before" "$(sha256sum nginx/certs/fullchain.pem 2>/dev/null | cut -d' ' -f1 || true)"
    [ $have_db = no ] || say "The database from before the rollback is kept as smp_before_restore (until the next restore)."
}

main "$@"
exit
