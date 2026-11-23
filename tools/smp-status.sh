#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

# Read-only view of a running SMP, for monitoring accounts that shouldn't be admins.
#
#   tools/smp-status.sh                 versions, containers, resources, health, disk, upgrades
#   tools/smp-status.sh logs SERVICE [LINES]
#                                       a service's recent log (server, nginx, dns-sinkhole, db;
#                                       LINES 1-2000, default 200)
#   tools/smp-status.sh agents          the PCs: agent version, addresses, online, last seen;
#                                       pending and rejected join requests
#
# Docker access can't be limited to viewing (whoever may use docker can mount the host's
# disk: that's root), so a monitoring account gets exactly this script through sudo, and
# nothing else (run as root, from /etc/sudoers.d/smp-status):
#
#   smp ALL=(root) NOPASSWD: /opt/smp/tools/smp-status.sh, /opt/smp/tools/smp-status.sh *
#
# The script must stay owned by root and not writable by that account (it is, inside the
# root-owned install folder). It only runs the fixed queries below, checks its arguments
# strictly, and never shows secrets: not .env, not `docker inspect` (the containers'
# environment holds the keys), no `docker exec` other than the fixed read-only SQL.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

dc() { docker compose "$@" </dev/null; }
env_get() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -n 1; }  # never `source` .env
heading() { printf '\n== %s\n' "$*"; }
usage() {
    echo "usage: $0 [status]" >&2
    echo "       $0 logs server|nginx|dns-sinkhole|db [LINES]" >&2
    echo "       $0 agents" >&2
    exit 2
}

status() {
    printf 'SMP status on %s, %s\n' "$(hostname)" "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    heading "Versions (Version.toml)"
    sh tools/version.sh --status 2>/dev/null || grep -E '^\[|^version' Version.toml
    heading "Containers"
    dc ps --all --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
    heading "Resources"
    local ids
    ids=$(dc ps -q)
    if [ -n "$ids" ]; then
        # shellcheck disable=SC2086  # one id per word
        docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}' $ids
    else
        echo "no containers running"
    fi
    heading "Health (through nginx)"
    local port
    port=$(env_get SMP_HTTPS_PORT)
    curl -sk -m 10 -H 'Host: localhost' "https://127.0.0.1:${port:-443}/healthz" || echo "no answer"
    echo
    heading "Disk"
    df -h / | tail -n +1
    docker system df
    heading "Upgrade backups (newest last)"
    local d found=no
    for d in backups/*-from-*/; do
        [ -d "$d" ] || continue
        found=yes
        printf '%s  from %s%s\n' "${d%/}" "$(cat "$d/installed-version" 2>/dev/null || echo '?')" \
            "$([ -e "$d/ROLLED-BACK" ] && echo ", rolled back $(cat "$d/ROLLED-BACK")")"
    done
    [ $found = yes ] || echo "none"
}

logs() {
    local service=$1 lines=${2:-200}
    case $service in server | nginx | dns-sinkhole | db) ;; *) usage ;; esac
    [[ $lines =~ ^[0-9]{1,4}$ ]] && [ "$lines" -ge 1 ] && [ "$lines" -le 2000 ] || usage
    dc logs --no-color --timestamps --tail "$lines" "$service"
}

agents() {
    # Fixed, read-only SQL. to_jsonb(d)->>'observed_ip' instead of the column itself:
    # works also on a database from before that column existed.
    dc exec -T db psql -U smp -d smp -P pager=off -v ON_ERROR_STOP=1 -c "
        SELECT d.hostname,
               coalesce(d.agent_version, '-') AS agent,
               coalesce(d.last_ip, '-') AS ip,
               coalesce(to_jsonb(d)->>'observed_ip', '-') AS seen_as,
               CASE WHEN d.last_seen_at > now() - interval '90 seconds' THEN 'online' ELSE 'offline' END AS state,
               coalesce(to_char(d.last_seen_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || ' UTC', 'never') AS last_seen
        FROM devices d WHERE d.agent_id IS NOT NULL ORDER BY d.hostname" \
        -c "SELECT count(*) FILTER (WHERE to_jsonb(r)->>'rejected_at' IS NULL) AS pending_join_requests,
                   count(*) FILTER (WHERE to_jsonb(r)->>'rejected_at' IS NOT NULL) AS rejected
            FROM agent_join_requests r WHERE r.expires_at > now()"
}

case ${1:-status} in
    status) [ $# -le 1 ] || usage; status ;;
    logs) [ $# -ge 2 ] && [ $# -le 3 ] || usage; logs "$2" "${3:-200}" ;;
    agents) [ $# -eq 1 ] || usage; agents ;;
    *) usage ;;
esac
