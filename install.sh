#!/usr/bin/env bash
# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

# SMP installer: first-time setup, and safe to re-run (it checks the configuration and
# rebuilds what is installed; newer versions are installed with ./upgrade.sh).
#
#   curl -fsSLO https://raw.githubusercontent.com/ticksparancsnok/SMP/main/install.sh
#   bash install.sh                  installs into this folder (empty, or --dir /opt/smp)
#   bash install.sh --help           options: another branch/tag, your own download, ...
#
# 0. gets SMP: downloads the newest version (the main branch on GitHub, or --ref /
#    --download-url) and unpacks it here, then continues with the installer that came
#    with it. Not in a git checkout (its files are used as they are), and not where SMP
#    is already installed (there it only says if ./upgrade.sh has something newer).
# 1. creates .env with random secrets (or checks the existing one), and works out
#    SMP's addresses (for nginx and its certificate) and the DNS sinkhole address
# 2. builds and starts the containers (nginx in front, HTTPS with a self-signed
#    certificate unless nginx/certs/ holds a real one) and waits until all is up.
#    The server image also compiles the Windows agent, offered at
#    https://<smp>/downloads/smp-agent.exe
# 3. on first setup: asks for the school name and the admin username, creates them,
#    and prints the admin's one-time password (changed at first sign-in)
#
# Answers can also be piped in (one per line), e.g. for automated test installs
# (`bash install.sh < answers`, not `curl ... | bash`: the script itself would be stdin).
set -euo pipefail

if [ -t 1 ]; then B=$'\e[1m' R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' N=$'\e[0m'; else B="" R="" G="" Y="" N=""; fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
warn() { printf '%s%s%s\n' "$Y" "$*" "$N"; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

ask() {  # ask VAR "question" [default]; dies if input ends with no default (no endless loops)
    local reply=""
    if ! read -r -p "$2${3:+ [$3]}: " reply && [ -z "$reply" ] && [ -z "${3:-}" ]; then
        printf '\n' >&2
        die "no answer to \"$2\" (input ended)"
    fi
    printf -v "$1" '%s' "${reply:-${3:-}}"
}
dc() { docker compose "$@" </dev/null; }  # docker must never eat the answers on stdin
yesno() {  # yesno "question" y|n   -> exit status 0 for yes
    local reply="" hint="y/N"
    [ "$2" = y ] && hint="Y/n"
    read -r -p "$1 [$hint]: " reply || true
    reply=${reply:-$2}
    [[ $reply =~ ^[Yy] ]]
}
secret() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n'; }  # 43 url-safe chars
env_get() { sed -n "s/^$1=//p" .env | tail -n 1; }                            # never `source` .env
env_set() {  # env_set KEY value  (values here never contain "|" or newlines)
    if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}
key_ok() { local k=$1; [ ${#k} -ge 32 ] && [ "$(printf '%s' "$k" | fold -w1 | sort -u | wc -l)" -ge 10 ]; }
is_ipv4() { [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
is_name() { [[ $1 =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; }
add_name() {  # add_name LIST NAME -> LIST with NAME appended once (comma separated)
    case ",$1," in *",$2,"*) printf '%s' "$1" ;; *) printf '%s' "${1:+$1,}$2" ;; esac
}

# ------------------------------------------------------------------ getting SMP
# Where SMP is downloaded from. A modified SMP served elsewhere: --download-url, or change
# these (keep the AGPL attribution, see NOTICE).
SMP_REPO_URL=https://github.com/ticksparancsnok/SMP
SMP_DEFAULT_REF=main
# Re-run where SMP is installed and a newer version exists: nothing is rebuilt, the
# admin is sent to ./upgrade.sh. Its own status, so scripts can tell it from a failure (1).
EXIT_NEWER_AVAILABLE=3

usage() {
    cat <<EOF
Usage: install.sh [options]

Installs SMP into a folder: downloads the newest version, sets it up, builds and starts it.
Run again where SMP is installed, it first checks GitHub for a newer version: if there is
one it stops (exit status $EXIT_NEWER_AVAILABLE, nothing rebuilt) and points to ./upgrade.sh;
otherwise it checks the configuration and rebuilds. Scripts that only want a rebuild,
without the check: ./install.sh --local.

  --dir DIR            install into DIR (default: the folder this script is in)
  --ref REF            a branch or tag of $SMP_REPO_URL (default: $SMP_DEFAULT_REF)
  --download-url URL   download this .tar.gz of SMP instead (e.g. a modified SMP)
  --sha256 HASH        refuse the download unless its SHA-256 is HASH
  --local              don't download: install the SMP files already in the folder
  --fetch DIR          only download and unpack SMP into DIR (used by upgrade.sh)
  -h, --help           this help
EOF
}

local_files=no
target=""
ref=$SMP_DEFAULT_REF
download_url=""
sha256=""
fetch_to=""
while [ $# -gt 0 ]; do
    case $1 in
        --local) local_files=yes ;;
        --dir | --ref | --download-url | --sha256 | --fetch)
            [ $# -ge 2 ] && [ -n "$2" ] || die "$1 needs a value (see --help)"
            case $1 in
                --dir) target=$2 ;;
                --ref) ref=$2 ;;
                --download-url) download_url=$2 ;;
                --sha256) sha256=$2 ;;
                --fetch) fetch_to=$2 ;;
            esac
            shift
            ;;
        -h | --help) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done
here=$(cd "$(dirname "$0")" && pwd)
target=${target:-$here}
url=${download_url:-$SMP_REPO_URL/archive/$ref.tar.gz}

# fetch_release DEST: downloads SMP and unpacks it into DEST (which must not exist yet).
# Prints why and returns 1 when that fails or the download isn't an SMP release.
fetch_release() {
    local dest=$1 tmp root entries
    command -v tar >/dev/null || { warn "tar is not installed"; return 1; }
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/smp-download.XXXXXX")
    say "downloading $url"
    if command -v curl >/dev/null; then
        curl -fsSL --retry 3 --max-filesize 500000000 -o "$tmp/smp.tar.gz" "$url" || { warn "download failed: $url"; rm -rf "$tmp"; return 1; }
    elif command -v wget >/dev/null; then
        wget -q -O "$tmp/smp.tar.gz" "$url" || { warn "download failed: $url"; rm -rf "$tmp"; return 1; }
    else
        warn "neither curl nor wget is installed"; rm -rf "$tmp"; return 1
    fi
    if [ -n "$sha256" ] && ! printf '%s  %s\n' "$sha256" "$tmp/smp.tar.gz" | sha256sum -c --quiet - >/dev/null 2>&1; then
        warn "the download's SHA-256 is $(sha256sum "$tmp/smp.tar.gz" | cut -d' ' -f1), not $sha256"
        rm -rf "$tmp"; return 1
    fi
    mkdir "$tmp/x"
    tar -xzf "$tmp/smp.tar.gz" -C "$tmp/x" --no-same-owner || { warn "the download isn't a .tar.gz archive"; rm -rf "$tmp"; return 1; }
    # GitHub's archives hold one folder (SMP-main/); take what's in it.
    root=$tmp/x
    entries=$(find "$tmp/x" -mindepth 1 -maxdepth 1 | wc -l)
    if [ "$entries" = 1 ] && [ -d "$(find "$tmp/x" -mindepth 1 -maxdepth 1)" ]; then
        root=$(find "$tmp/x" -mindepth 1 -maxdepth 1)
    fi
    if [ ! -f "$root/Version.toml" ] || [ ! -f "$root/tools/release.sh" ]; then
        warn "the download is not an SMP release this installer can use (no Version.toml / tools/release.sh: SMP before 1.0.2 C?)"
        rm -rf "$tmp"; return 1
    fi
    mkdir -p "$(dirname "$dest")"
    mv "$root" "$dest"
    rm -rf "$tmp"
    # shellcheck source=tools/release.sh
    source "$dest/tools/release.sh"
    smp_check_release "$dest" || { rm -rf "$dest"; return 1; }
    sh "$dest/tools/version.sh" release "$dest/Version.toml" >/dev/null || { rm -rf "$dest"; return 1; }
}

if [ -n "$fetch_to" ]; then  # upgrade.sh: download only
    [ ! -e "$fetch_to" ] || die "--fetch: $fetch_to already exists"
    fetch_release "$fetch_to" || die "couldn't download SMP"
    exit 0
fi

if [ $local_files = no ]; then
    step "Getting SMP"
    if [ -e "$target/.git" ]; then
        say "$target is a git checkout: installing its files as they are (update them with git)"
    elif [ -f "$target/Version.toml" ] && [ -f "$target/docker-compose.yml" ]; then
        # SMP is here already: this run checks and rebuilds it. A newer version is
        # upgrade.sh's job (it shows what changes and keeps the replaced files).
        installed=$(sh "$target/tools/version.sh" release "$target/Version.toml" 2>/dev/null || true)
        say "SMP ${installed:-(unknown version)} is installed in $target; checking for a newer one"
        work=$(mktemp -d "${TMPDIR:-/tmp}/smp-check.XXXXXX")
        if fetch_release "$work/smp"; then
            offered=$(sh "$work/smp/tools/version.sh" release "$work/smp/Version.toml")
            newer=$(sh "$work/smp/tools/version.sh" --compare "${installed:-0.0.0}" "$offered" 2>/dev/null || true)
            rm -rf "$work"
            if [ "$newer" = lt ]; then
                say "SMP $offered is available."
                say "  ./upgrade.sh          installs it (shows what changes first, keeps the replaced files)"
                say "  ./install.sh --local  rebuilds the installed $installed as it is"
                exit "$EXIT_NEWER_AVAILABLE"
            fi
            say "that is the newest one ($offered); rebuilding what is installed"
        else
            rm -rf "$work"
            warn "couldn't check for a newer version; rebuilding what is installed"
        fi
    else
        mkdir -p "$target"
        target=$(cd "$target" && pwd)
        # Only into a fresh folder: anything else there could be overwritten.
        others=$(find "$target" -mindepth 1 -maxdepth 1 ! -name install.sh ! -name upgrade.sh ! -name .env ! -name nginx -printf '%f ' 2>/dev/null)
        if [ -n "$others" ]; then
            die "$target isn't empty (${others% }): install into an empty folder, e.g. bash install.sh --dir /opt/smp"
        fi
        work=$(mktemp -d "${TMPDIR:-/tmp}/smp-install.XXXXXX")
        fetch_release "$work/smp" || { rm -rf "$work"; die "couldn't download SMP (see above); --download-url / --ref choose another source"; }
        # shellcheck source=tools/release.sh
        source "$work/smp/tools/release.sh"
        smp_plan "$work/smp" "$target" "$work/plan" || { rm -rf "$work"; die "couldn't unpack SMP"; }
        smp_apply "$work/smp" "$target" "$work/plan" || { rm -rf "$work"; die "couldn't copy SMP into $target"; }
        say "SMP $(sh "$work/smp/tools/version.sh" release "$work/smp/Version.toml") unpacked into $target"
        rm -rf "$work"
    fi
    # Continue with the installer of the files being installed (maybe newer than this one).
    exec bash "$target/install.sh" --local --dir "$target"
fi
[ -f "$target/docker-compose.yml" ] && [ -f "$target/.env.example" ] || die "$target has no SMP files (run without --local to download them)"
cd "$target"

# ------------------------------------------------------------------ prerequisites
step "Checking prerequisites"
command -v docker >/dev/null || die "docker is not installed"
docker compose version </dev/null >/dev/null 2>&1 || die "the docker compose plugin is missing"
docker info </dev/null >/dev/null 2>&1 || die "can't talk to the docker daemon (is it running? are you in the docker group?)"
[ -f .env.example ] || die ".env.example not found (run this from the SMP directory)"
project=$(dc config --no-interpolate 2>/dev/null | sed -n 's/^name: //p' | head -n 1)
project=${project:-smp}
volume="${project}_pgdata"
db_exists=no
docker volume inspect "$volume" >/dev/null 2>&1 && db_exists=yes
lan=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
[ -n "$lan" ] || lan=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' || true)
host_short=$(hostname 2>/dev/null || true)
host_fqdn=$(hostname -f 2>/dev/null || true)
say "docker OK; database volume $volume: $([ $db_exists = yes ] && echo exists || echo "not created yet"); LAN address: ${lan:-unknown}"

# ------------------------------------------------------------------ .env: secrets
step "Configuration (.env)"
if [ ! -f .env ]; then
    if [ $db_exists = yes ]; then
        warn "There is an existing SMP database ($volume), but no .env file."
        say  "The .env holds the database password and the secret key, and a new one won't"
        say  "match the existing database (the server would refuse to start). Either:"
        say  "  - restore your old .env next to install.sh and run ./install.sh again, or"
        say  "  - start over: this DELETES ALL SMP DATA (users, settings, audit log)."
        answer=""
        ask answer "Type DELETE to start over, or press Enter to stop"
        [ "$answer" = DELETE ] || die "stopped; restore your .env and run ./install.sh again"
        ids=$(docker ps -aq --filter "volume=$volume" </dev/null)
        [ -n "$ids" ] && docker rm -f $ids >/dev/null
        docker volume rm "$volume" >/dev/null
        db_exists=no
        say "old database removed"
    fi
    cp .env.example .env
    chmod 600 .env
    env_set SMP_DB_PASSWORD "$(secret)"
    env_set SMP_SECRET_KEY "$(secret)"
    env_set SMP_DNS_SYNC_TOKEN "$(secret)"
    env_set SMP_BACKUP_KEY "$(secret)"
    env_set SMP_DNS_SINKHOLE_URL "http://dns-sinkhole:8080"
    say "created .env with new random secrets (keep a copy away from this server: without it the database and its backups can't be opened)"
else
    say "using the existing .env"
    if ! key_ok "$(env_get SMP_SECRET_KEY)"; then
        warn "SMP_SECRET_KEY in .env is missing, too short or a placeholder (the server refuses to start with it)."
        yesno "Generate a new random one?" y || die "set SMP_SECRET_KEY in .env (at least 32 random characters) and run again"
        env_set SMP_SECRET_KEY "$(secret)"
    fi
    if [ -z "$(env_get SMP_DNS_SYNC_TOKEN)" ]; then
        env_set SMP_DNS_SYNC_TOKEN "$(secret)"
        say "generated SMP_DNS_SYNC_TOKEN"
    fi
    # Only when missing: replacing it would make every existing backup unreadable.
    if [ -z "$(env_get SMP_BACKUP_KEY)" ]; then
        env_set SMP_BACKUP_KEY "$(secret)"
        say "generated SMP_BACKUP_KEY (encrypts the database backups ./upgrade.sh makes)"
    elif ! key_ok "$(env_get SMP_BACKUP_KEY)"; then
        die "SMP_BACKUP_KEY in .env is too short or a placeholder: set it to at least 32 random characters (existing backups need the key they were made with)"
    fi
    if [ -z "$(env_get SMP_DB_PASSWORD)" ] || [ "$(env_get SMP_DB_PASSWORD)" = change-me ]; then
        [ $db_exists = yes ] && die "SMP_DB_PASSWORD in .env is empty/placeholder but a database exists: put its original password back"
        env_set SMP_DB_PASSWORD "$(secret)"
        say "generated SMP_DB_PASSWORD"
    fi
fi
# nginx serves SMP over HTTPS, so the session cookie is always HTTPS-only.
if [ "$(env_get SMP_COOKIE_SECURE)" != true ]; then
    env_set SMP_COOKIE_SECURE true
    say "SMP_COOKIE_SECURE=true (nginx serves SMP over HTTPS now)"
fi

# ------------------------------------------------------------------ .env: addresses
step "Addresses"
names=$(env_get SMP_SERVER_NAMES)
if [ -z "$names" ]; then
    names="localhost,127.0.0.1"
    for n in "$host_short" "$host_fqdn" "$lan"; do
        [ -n "$n" ] && is_name "$n" && names=$(add_name "$names" "$n")
    done
    say "SMP will answer to: ${names//,/, }"
    say "Requests for any other name are treated as blocked sites (they get the block page)."
    while :; do
        extra=""
        ask extra "Other names people type to reach SMP, e.g. smp.iskola.hu (comma separated, Enter for none)" "-"
        [ "$extra" = "-" ] && break
        ok=yes
        for n in ${extra//,/ }; do
            if is_name "$n"; then names=$(add_name "$names" "$n"); else warn "not a valid name: $n"; ok=no; fi
        done
        [ $ok = yes ] && break
    done
    env_set SMP_SERVER_NAMES "$names"
else
    say "SMP answers to (SMP_SERVER_NAMES in .env): ${names//,/, }"
fi

sinkhole=$(env_get SMP_SINKHOLE_IP)
if [ -z "$sinkhole" ]; then
    say "The school's computers will use this server's LAN address as their DNS server,"
    say "and blocked sites resolve to it (nginx shows the block page there). Give the"
    say "server a fixed address (e.g. a DHCP reservation), or the clients lose DNS when it changes."
    while :; do
        ask sinkhole "This server's LAN IPv4 address" "${lan:-127.0.0.1}"
        is_ipv4 "$sinkhole" && break
        warn "not an IPv4 address: $sinkhole"
    done
    env_set SMP_SINKHOLE_IP "$sinkhole"
else
    say "DNS / block page address (SMP_SINKHOLE_IP in .env): $sinkhole"
fi
dns_port=$(env_get SMP_DNS_PORT)
dns_port=${dns_port:-53}

# Port 53 must be free on that address (unless our own DNS container already holds it).
# A program listening on all addresses (0.0.0.0 / [::]) or on this one blocks it;
# systemd-resolved's usual 127.0.0.53 / 127.0.0.54 doesn't.
if command -v ss >/dev/null && [ -z "$(dc ps -q dns-sinkhole 2>/dev/null)" ]; then
    taken=$(ss -H -ln -u -t "sport = :$dns_port" 2>/dev/null | awk '{print $5}' \
        | grep -E "^(0\.0\.0\.0|\*|\[::\]|${sinkhole//./\\.}):$dns_port$" | sort -u | tr '\n' ' ' || true)
    if [ -n "$taken" ]; then
        warn "Port $dns_port on $sinkhole is already in use by another program (listening on: $taken)."
        say  "SMP's DNS needs it, because Windows clients can only use port 53. Stop that program"
        say  "(often dnsmasq, bind/named, or a DNS server enabled for the whole network), or for a"
        say  "test machine set SMP_DNS_PORT=1053 in .env. Then run ./install.sh again."
        die "port $dns_port is taken on $sinkhole"
    fi
fi
mkdir -p nginx/certs

# ------------------------------------------------------------------ start
subnet=$(env_get SMP_DOCKER_SUBNET)
subnet=${subnet:-172.31.255.0/24}
current=$(docker network inspect "${project}_default" -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null </dev/null || true)
if [ -n "$current" ] && [ "$current" != "$subnet" ]; then
    say "recreating the docker network with subnet $subnet (was ${current}); data is kept"
    dc down >/dev/null
fi

# Building and starting are separate steps, so a failure says which one it was: a build
# fails on downloads (no internet, DNS, proxy), starting fails on ports in use.
if [ -f tools/preflight.sh ]; then
    # shellcheck source=tools/preflight.sh
    source tools/preflight.sh
    if ! smp_check_registries; then
        warn "Docker can't reach the package registries above right now. Parts of SMP that changed"
        warn "since the last build can't be built without them (check this server's DNS, firewall or proxy)."
    fi
fi
step "Building the containers (the first build compiles the Windows agent too: allow 5-10 minutes)"
if ! dc build; then
    die "building failed (see above). Most often a download during the build (\"Could not find a version\", \"timed out\", \"Temporary failure in name resolution\"): this server can't reach the package registries (PyPI, npm, Rust crates, Go modules); check its DNS, firewall or proxy, then run ./install.sh again. SMP as it was keeps running."
fi
step "Starting the containers"
if ! dc up -d; then
    die "starting the containers failed (see above). If a port is already in use on this machine: SMP_HTTP_PORT / SMP_HTTPS_PORT (80/443) and SMP_DNS_PORT (53) in .env, then run ./install.sh again"
fi

step "Waiting for the server"
health='import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=2)'
started=no
for _ in $(seq 1 90); do
    if dc logs --no-log-prefix server 2>/dev/null | grep -q "SMP can't start"; then
        break
    fi
    if dc exec -T server python -c "$health" >/dev/null 2>&1; then
        started=yes
        break
    fi
    sleep 2
done
if [ $started != yes ]; then
    printf '\n%sThe server did not start.%s\n' "$R" "$N"
    report=$(dc logs --no-log-prefix server 2>/dev/null | sed -n "/SMP can't start/,/^=====/p" | tail -n 40)
    if [ -n "$report" ]; then say "$report"; else dc logs --no-log-prefix --tail 40 server; fi
    dc stop server >/dev/null 2>&1 || true  # don't leave it in a restart loop
    die "fix the problem above, then run ./install.sh again (server container stopped)"
fi
say "${G}server is up${N}"

proxied=no
for _ in $(seq 1 30); do
    if dc exec -T nginx wget -q -O /dev/null --no-check-certificate --header "Host: localhost" https://127.0.0.1/healthz >/dev/null 2>&1; then
        proxied=yes
        break
    fi
    sleep 2
done
if [ $proxied != yes ]; then
    dc logs --no-log-prefix --tail 30 nginx
    die "nginx doesn't pass requests to the server (log above)"
fi
say "${G}nginx is up${N} ($(dc logs --no-log-prefix nginx 2>/dev/null | sed -n 's/^smp: //p' | tail -n 1))"

# ------------------------------------------------------------------ first-time setup
status=$(dc exec -T server smp status) || die "the server can't reach its database: $status"
school=$(sed -n 's/^school_name=//p' <<<"$status")
created=no
if [ "$(sed -n 's/^bootstrapped=//p' <<<"$status")" = yes ]; then
    step "Already set up for: $school"
    say "nothing else to do (containers rebuilt and migrated)"
else
    step "First-time setup"
    school=""
    while [ -z "$school" ]; do ask school "School name (shown in the portal)"; done
    while :; do
        ask admin "Administrator username" admin
        [[ $admin =~ ^[A-Za-z0-9._@-]{1,128}$ ]] && break
        warn "use letters, digits and . _ @ - only (no spaces or accents)"
    done
    out=$(dc exec -T server smp bootstrap --otp --machine --org-name "$school" --admin-username "$admin") \
        || die "creating the school/admin failed: $out"
    admin=$(sed -n 's/^SMP_ADMIN_USERNAME=//p' <<<"$out")
    otp=$(sed -n 's/^SMP_ADMIN_OTP=//p' <<<"$out")
    until=$(sed -n 's/^SMP_ADMIN_OTP_VALID_UNTIL=//p' <<<"$out")
    created=yes
fi

# The DNS container keeps its lists on a volume, but a new or emptied volume (or a
# restored database) needs them pushed again; the server only pushes on changes.
if sync=$(dc exec -T server smp dns-sync 2>&1); then
    say "$sync"
else
    warn "couldn't send the DNS lists to the sinkhole yet; they go out with the next blocklist change ($sync)"
fi

# ------------------------------------------------------------------ summary
https_port=$(env_get SMP_HTTPS_PORT)
https_port=${https_port:-443}
suffix=""
[ "$https_port" = 443 ] || suffix=":$https_port"
step "Done"
say "School:      $school"
say "Version:     SMP $(sh tools/version.sh release)"
say "Open SMP:    https://localhost$suffix/"
for n in ${names//,/ }; do
    case $n in localhost|127.0.0.1) ;; *) say "             https://$n$suffix/" ;; esac
done
first_name=localhost
for n in ${names//,/ }; do
    case $n in localhost|127.0.0.1) ;; *) first_name=$n; break ;; esac
done
say ""
say "PC agent:    https://$first_name$suffix/downloads/smp-agent.exe   (Windows 10/11; built with this server)"
say "             on each PC, as administrator: smp-agent.exe install --server $first_name$suffix"
say "             (also in SMP: Devices -> Agent for Windows)"
say ""
if [ "$dns_port" = 53 ]; then
    say "DNS server:  $sinkhole   (give this to the school's computers as their only DNS server)"
    say "             Easiest: hand it out via the school's DHCP (DNS server option) instead of"
    say "             setting each PC. Don't add a public DNS as secondary: clients would use it"
    say "             to get around the filter."
else
    warn "DNS server:  $sinkhole port $dns_port (test setting: clients need port 53; set SMP_DNS_PORT=53 in .env)"
fi
if grep -qs "SMP self-signed" <(dc exec -T nginx openssl x509 -in /etc/nginx/certs/fullchain.pem -noout -subject 2>/dev/null); then
    say ""
    warn "The certificate is self-signed: browsers warn once per device; accept it to continue."
    say  "For a real one, put fullchain.pem + privkey.pem into nginx/certs/ and run: docker compose restart nginx"
fi
if [ $created = yes ]; then
    printf '\n%s  Administrator:       %s%s\n' "$B" "$admin" "$N"
    printf '%s  One-time password:   %s%s\n' "$B" "$otp" "$N"
    say "  valid until:         $until"
    say ""
    say "Sign in with these; SMP then asks you to choose your own password."
    say "The one-time password is shown only here (and in Users -> Export -> Login details until it's used)."
fi
