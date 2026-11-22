#!/usr/bin/env bash
# SMP installer: first-time setup, and safe to re-run for upgrades.
#
#   ./install.sh
#
# 1. creates .env with random secrets (or checks the existing one), and works out
#    SMP's addresses (for nginx and its certificate) and the DNS sinkhole address
# 2. builds and starts the containers (nginx in front, HTTPS with a self-signed
#    certificate unless nginx/certs/ holds a real one) and waits until all is up.
#    The server image also compiles the Windows agent, offered at
#    https://<smp>/downloads/smp-agent.exe
# 3. on first setup: asks for the school name and the admin username, creates them,
#    and prints the admin's one-time password (changed at first sign-in)
#
# Answers can also be piped in (one per line), e.g. for automated test installs.
set -euo pipefail
cd "$(dirname "$0")"

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
    env_set SMP_DNS_SINKHOLE_URL "http://dns-sinkhole:8080"
    say "created .env with new random secrets (keep a backup: without it the database can't be opened)"
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

step "Building and starting the containers (the first build compiles the Windows agent too: allow 5-10 minutes)"
if ! dc up -d --build; then
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
