#!/bin/sh
# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

# Reads and compares SMP versions (Version.toml is the only place they're kept).
#
#   tools/version.sh <section> [Version.toml]   one component's version
#                                                e.g. tools/version.sh agent  ->  1.0.3 B
#   tools/version.sh --compare <a> <b>           prints lt, eq or gt (a compared with b)
#                                                e.g. --compare "1.0.2 C" 1.0.3  ->  lt
#   tools/version.sh --status [Version.toml]     every component against [release]:
#                                                up to date, ahead (in development), behind
#
# Sections: server, WEBUI, agent, Site-blocker, release. Versions look like "1.0.2" or
# "1.0.2 C": numbers compare as numbers, and a revision letter comes after the plain
# version (1.0.2 < 1.0.2 A < 1.0.2 B < 1.0.3).
# Plain sh + awk, so the Docker build stages (Debian images) and install.sh/upgrade.sh
# can use it without Python. Fails (exit 1, message on stderr) when the file, the
# section or its version is missing, or a version to compare is malformed.
set -eu

usage() {
    echo "usage: $0 <section> [Version.toml]" >&2
    echo "       $0 --compare <version> <version>" >&2
    echo "       $0 --status [Version.toml]" >&2
    exit 2
}

# "1.0.2 C" -> "1 0 2 C" (letter "-" when there is none); fails for anything else.
split_version() {
    printf '%s\n' "$1" | awk '
        /^[0-9]+\.[0-9]+\.[0-9]+( [A-Z])?$/ {
            split($1, n, ".")
            printf "%d %d %d %s\n", n[1], n[2], n[3], ($2 == "" ? "-" : $2)
            ok = 1
        }
        END { if (!ok) exit 1 }
    ' || { echo "$0: \"$1\" is not a version like \"1.0.2\" or \"1.0.2 C\"" >&2; exit 1; }
}

if [ "${1:-}" = --compare ]; then
    [ $# -eq 3 ] || usage
    a=$(split_version "$2")
    b=$(split_version "$3")
    # "-" sorts before "A" in awk's string comparison, so a plain version comes first.
    printf '%s\n%s\n' "$a" "$b" | awk '
        NR == 1 { for (i = 1; i <= 4; i++) a[i] = $i }
        NR == 2 {
            for (i = 1; i <= 3; i++) {
                if (a[i] + 0 < $i + 0) { print "lt"; exit }
                if (a[i] + 0 > $i + 0) { print "gt"; exit }
            }
            print (a[4] < $4 ? "lt" : (a[4] > $4 ? "gt" : "eq"))
        }
    '
    exit 0
fi

# Components built with the release follow its number; the letter counts development
# since (see the top of Version.toml). Site-blocker is versioned on its own.
if [ "${1:-}" = --status ]; then
    [ $# -le 2 ] || usage
    file=${2:-$(dirname "$0")/../Version.toml}
    release=$(sh "$0" release "$file") || exit 1
    printf '%-13s %-9s %s\n' release "$release" "the release"
    for s in server WEBUI agent Site-blocker; do
        v=$(sh "$0" "$s" "$file") || exit 1
        if [ "$s" = Site-blocker ]; then
            state="own versioning (not tied to the release)"
        else
            case $(sh "$0" --compare "$v" "$release") in
                eq) state="up to date" ;;
                gt) state="ahead (in development)" ;;
                *) state="behind the release" ;;
            esac
        fi
        printf '%-13s %-9s %s\n' "$s" "$v" "$state"
    done
    exit 0
fi

[ $# -ge 1 ] && [ $# -le 2 ] || usage
section=$1
file=${2:-$(dirname "$0")/../Version.toml}
[ -f "$file" ] || { echo "$0: $file not found" >&2; exit 1; }

# Line based, like the readers in build.rs and vite.config.ts: "[section]" (a comment may
# follow) starts a section; `version = "..."` inside it is the answer.
awk -v want="$section" '
    /^[ \t]*\[/ {
        name = $0
        sub(/^[ \t]*\[[ \t]*/, "", name)
        sub(/[ \t]*\].*$/, "", name)
        in_section = (name == want)
        next
    }
    in_section && /^[ \t]*version[ \t]*=[ \t]*"/ {
        v = $0
        sub(/^[^"]*"/, "", v)
        sub(/".*$/, "", v)
        print v
        found = 1
        exit
    }
    END { if (!found) exit 1 }
' "$file" || { echo "$0: no version for [$section] in $file" >&2; exit 1; }
