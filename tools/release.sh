# shellcheck shell=bash
# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

# Putting a downloaded SMP release into an install folder: sourced (bash) by install.sh
# (first install) and upgrade.sh. They always source the copy from the NEW release, so
# this logic is the newest one when it runs.
#
#   smp_check_release DIR                  is DIR an unpacked SMP release? (else returns 1)
#   smp_plan NEW TARGET PLAN               what installing NEW into TARGET would change,
#                                          as lists in the folder PLAN (see below)
#   smp_apply NEW TARGET PLAN [BACKUP]     do it; with BACKUP, keep the old copy of every
#                                          file it replaces or removes there first
#   smp_restore_files BACKUP TARGET        undo smp_apply from its BACKUP (upgrade.sh --rollback)
#
# The install folder keeps `.smp-manifest`: "sha256  path" of every file the installed
# release brought (sha256sum format). With it an upgrade knows which files a newer
# release dropped (they're removed: a stale database migration would be dangerous) and
# which files someone changed by hand (they're backed up before being replaced).
# Without a manifest (a folder not installed by these scripts) nothing is removed.
#
# Never written or removed, whatever a release contains (smp_protected):
#   .env                the secrets and settings
#   nginx/certs/        the HTTPS certificate. The PC agents pin the self-signed one
#                       (agent/smp-agent/src/core/tls.rs): a new one would cut off every
#                       installed agent
#   backups/            previous files kept by upgrades
#   .smp-manifest       written by smp_apply itself
#
# Plan lists (one path per line, relative to TARGET): added, changed, removed (files of
# the old release the new one doesn't have), modified (files of the old release changed
# since it was installed), and manifest (the new .smp-manifest).

SMP_MANIFEST=.smp-manifest
SMP_REQUIRED_FILES=(Version.toml docker-compose.yml install.sh upgrade.sh tools/version.sh tools/release.sh
    tools/db-backup.sh server/Dockerfile .env.example)

smp_protected() {
    case $1 in
        .env | .smp-manifest | nginx/certs/* | backups/*) return 0 ;;
        *) return 1 ;;
    esac
}

smp_check_release() {
    local f
    for f in "${SMP_REQUIRED_FILES[@]}"; do
        [ -f "$1/$f" ] || { echo "not an SMP release: $f is missing" >&2; return 1; }
    done
}

# "sha256  path" for every regular file under DIR (protected paths and .git left out),
# sorted by path.
_smp_hash_tree() {
    (
        cd "$1" || exit 1
        find . -path ./.git -prune -o -type f -print0 | LC_ALL=C sort -z | while IFS= read -r -d '' f; do
            f=${f#./}
            smp_protected "$f" || printf '%s\0' "$f"
        done | xargs -0 -r sha256sum --
    )
}

smp_plan() {
    local new=$1 target=$2 plan=$3 h p
    mkdir -p "$plan"
    : >"$plan/added"; : >"$plan/changed"; : >"$plan/removed"; : >"$plan/modified"

    # Paths must be plain: no newlines (one per line here) and nothing outside the folder.
    if (cd "$new" && find . -path ./.git -prune -o -print0) | tr '\0' '\n' | grep -q -E '(^|/)\.\.(/|$)'; then
        echo "the release contains paths with '..'" >&2
        return 1
    fi
    if (cd "$new" && find . -path ./.git -prune -o -type l -print | grep -q .); then
        echo "the release contains symbolic links, which SMP releases never do" >&2
        return 1
    fi
    _smp_hash_tree "$new" >"$plan/manifest" || return 1

    local -A new_hash=() old_hash=() current=()
    while read -r h p; do new_hash[$p]=$h; done <"$plan/manifest"
    if [ -f "$target/$SMP_MANIFEST" ]; then
        while read -r h p; do
            smp_protected "$p" || old_hash[$p]=$h
        done <"$target/$SMP_MANIFEST"
    fi
    # Current hashes of every target file either release knows about.
    local -a known=()
    for p in "${!new_hash[@]}" "${!old_hash[@]}"; do
        [ -f "$target/$p" ] && known+=("$p")
    done
    if [ ${#known[@]} -gt 0 ]; then
        while read -r h p; do current[$p]=$h; done < <(cd "$target" && printf '%s\0' "${known[@]}" | sort -zu | xargs -0 sha256sum --)
        # A file that exists but couldn't be read would otherwise look absent ("added"),
        # and be overwritten without being backed up first.
        for p in "${known[@]}"; do
            [ -n "${current[$p]:-}" ] || { echo "can't read $target/$p (check its owner and permissions)" >&2; return 1; }
        done
    fi

    for p in "${!new_hash[@]}"; do
        if [ -z "${current[$p]:-}" ]; then
            echo "$p" >>"$plan/added"
        elif [ "${current[$p]}" != "${new_hash[$p]}" ]; then
            echo "$p" >>"$plan/changed"
        fi
    done
    for p in "${!old_hash[@]}"; do
        [ -n "${current[$p]:-}" ] || continue  # already gone
        [ -n "${new_hash[$p]:-}" ] || echo "$p" >>"$plan/removed"
        [ "${current[$p]}" = "${old_hash[$p]}" ] || echo "$p" >>"$plan/modified"
    done
    local l
    for l in added changed removed modified; do LC_ALL=C sort -o "$plan/$l" "$plan/$l"; done
}

smp_apply() {
    # Every step returns 1 on failure: callers test the result (`if ! smp_apply`, `||`),
    # and in that position bash ignores `set -e` inside the function, so a failed copy
    # (e.g. a full disk) would otherwise go unnoticed and the upgrade carry on.
    local new=$1 target=$2 plan=$3 backup=${4:-} p
    if [ -n "$backup" ]; then
        mkdir -p "$backup" || return 1
        while IFS= read -r p; do
            mkdir -p "$backup/$(dirname "$p")" && cp -p -- "$target/$p" "$backup/$p" \
                || { echo "couldn't back up $p" >&2; return 1; }
        done < <(cat "$plan/changed" "$plan/removed")
        local l
        for l in added changed removed modified; do cp "$plan/$l" "$backup/$l-files.txt" || return 1; done
        [ ! -f "$target/$SMP_MANIFEST" ] || cp -p "$target/$SMP_MANIFEST" "$backup/" || return 1
    fi
    # Copy next to the old file, then rename over it: a script that is running right now
    # (upgrade.sh, install.sh) keeps reading its old copy instead of a half-written one.
    while IFS= read -r p; do
        mkdir -p "$target/$(dirname "$p")" && cp -p -- "$new/$p" "$target/$p.smp-new" \
            && mv -f -- "$target/$p.smp-new" "$target/$p" \
            || { rm -f -- "$target/$p.smp-new"; echo "couldn't write $p" >&2; return 1; }
    done < <(cat "$plan/added" "$plan/changed")
    local d
    while IFS= read -r p; do
        rm -f -- "$target/$p" || { echo "couldn't remove $p" >&2; return 1; }
        d=$(dirname "$p")  # and the folders that became empty, inside TARGET only
        while [ "$d" != . ] && rmdir -- "$target/$d" 2>/dev/null; do d=$(dirname "$d"); done
    done <"$plan/removed"
    cp "$plan/manifest" "$target/$SMP_MANIFEST.smp-new" && mv -f "$target/$SMP_MANIFEST.smp-new" "$target/$SMP_MANIFEST"
}

smp_restore_files() {
    local backup=$1 target=$2 p d l
    for l in added changed removed; do
        [ -f "$backup/$l-files.txt" ] || { echo "$backup has no $l-files.txt: not a backup made by upgrade.sh" >&2; return 1; }
    done
    while IFS= read -r p; do
        [ -f "$backup/$p" ] || { echo "$backup/$p is missing" >&2; return 1; }
    done < <(cat "$backup/changed-files.txt" "$backup/removed-files.txt")
    while IFS= read -r p; do
        mkdir -p "$target/$(dirname "$p")" && cp -p -- "$backup/$p" "$target/$p.smp-new" \
            && mv -f -- "$target/$p.smp-new" "$target/$p" \
            || { rm -f -- "$target/$p.smp-new"; echo "couldn't put back $p" >&2; return 1; }
    done < <(cat "$backup/changed-files.txt" "$backup/removed-files.txt")
    while IFS= read -r p; do
        rm -f -- "$target/$p" || { echo "couldn't remove $p" >&2; return 1; }
        d=$(dirname "$p")
        while [ "$d" != . ] && rmdir -- "$target/$d" 2>/dev/null; do d=$(dirname "$d"); done
    done <"$backup/added-files.txt"
    if [ -f "$backup/$SMP_MANIFEST" ]; then
        cp "$backup/$SMP_MANIFEST" "$target/$SMP_MANIFEST.smp-new" && mv -f "$target/$SMP_MANIFEST.smp-new" "$target/$SMP_MANIFEST"
    fi
}
