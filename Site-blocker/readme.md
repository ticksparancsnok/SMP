# DNS Sinkhole & Content Filtering Service (DNS Resolver Service)

## Running the local MVP

The `school-dns-demo` directory contains the local DNS sinkhole and block page
demo. The service is reachable over two protocols:

| Service | Default address |
|---|---|
| CoreDNS DNS service | `127.0.0.1:1053` |
| HTTP block page | `127.0.0.1:8080` |

The `schoolfilter` plugin reads `blocklist.json` at startup and keeps it in
memory, so the DNS hot path never touches the filesystem or the control plane.

## SMP API and web UI integration

The Go sinkhole also accepts control-plane synchronization:

```text
POST /api/blocklist/sync
Authorization: Bearer <DNS_SYNC_TOKEN>
```

The SMP Python API stores entries in PostgreSQL and synchronizes the complete
canonical `entries` (blocklist) and `allowlist` arrays after every change. The Go
service replaces its in-memory state and writes both to `blocklist.json`.

Decision order for every query (`Match` in `blocklist.go`):

1. **allowlist**: the domain or a parent with `includeSubdomains` → allowed
2. **blocklist**: same matching → blocked (sinkhole answer, block page)
3. anything else → allowed

Public suffixes (com, hu, co.uk, …) never reach either list: the control plane
refuses them (Public Suffix List via `tldextract`).

The web UI route is `/dns`. Manual entries and imported hosts are not accepted
with a client-controlled `addedBy` value:

- manual entry: the authenticated SMP username
- hosts import: `imported by: <username>`
- API token caller: `token:<token name>` (if no browser user is present)

The Go status endpoint is:

```text
GET /api/blocklist/status
Authorization: Bearer <DNS_SYNC_TOKEN>
```

**Every `/api/blocklist/*` endpoint** (`sync`, `status`, `import`, `entries`)
requires `Authorization: Bearer <DNS_SYNC_TOKEN>`. Port 8080 also serves the
block page, so the API must not be open.

**In the Docker stack** DNS is published on **port 53 of the server's LAN address**
(`SMP_SINKHOLE_IP`; clients, Windows in particular, can only use port 53). Only that
address, because `0.0.0.0:53` usually collides with the host's own resolver
(systemd-resolved on 127.0.0.53). Inside the container CoreDNS still listens on 1053,
so it needs no privileges. `SMP_DNS_PORT` changes the published port for testing.

The synced lists are saved to `/app/data/blocklist.json` on the `dnsdata` volume
(`BLOCKLIST_PATH` overrides the Corefile's path), so a rebuilt or restarted container
keeps filtering without the server. With no file yet (a new volume) it starts with
empty lists, never the demo entries; `smp dns-sync` (run by `./install.sh`) pushes the
database's lists.

Port 8080 isn't published either: nginx forwards every request
for a name that isn't SMP's own (`SMP_SERVER_NAMES`) to it, on ports 80 and 443, so
students see the block page when they open a blocked site. For that, blocked names
must resolve to the server's LAN address: compose passes `SMP_SINKHOLE_IP` as
`SINKHOLE_IP`, which overrides the Corefile's `127.0.0.1` (only right for a local
demo). Blocked HTTPS sites show the browser's certificate warning first; sites on
browsers' HSTS preload lists (Google, YouTube, ...) can't show the page at all and
just fail to load, which still blocks them.
If `DNS_SYNC_TOKEN` is not set, the API answers `503` (disabled, not open);
DNS filtering and the block page keep working.

Docker Compose configuration uses `SMP_DNS_SINKHOLE_URL`,
`SMP_DNS_SYNC_TOKEN`, and the Go container's `DNS_SYNC_TOKEN`. Compose refuses
to start without `SMP_DNS_SYNC_TOKEN` and passes the same value to both sides.

### Deploying with IntelliJ SFTP deployment

Set the target path of the IntelliJ Deployment configuration to:

```text
/home/vboxuser/school-dns-demo
```

These local project files and directories must be uploaded to the VM:

```text
school-dns-demo/
├── Corefile
├── blocklist.json
├── go.mod
├── go.sum
├── main.go
└── plugin/
    └── schoolfilter/
        ├── blocklist.go
        ├── blocklist_test.go
        ├── blockpage.html       the block page in Hungarian
        ├── blockpage.en.html    the same in English (keep the two in step)
        ├── hostsimport.go
        ├── schoolfilter.go
        └── schoolfilter_test.go
```

In IntelliJ, use **Deployment → Upload to Debian**. Make sure the `plugin` and
`schoolfilter` directories are not listed under **Excluded Paths**.

### Building and running on the Debian VM

```bash
cd ~/school-dns-demo
go mod tidy
go test ./...
go build -ldflags "-X 'school-dns-demo/plugin/schoolfilter.Version=$(sh ../tools/version.sh Site-blocker ../Version.toml)'" .
export DNS_SYNC_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
./school-dns-demo
```

Without `DNS_SYNC_TOKEN` the `/api/blocklist/*` endpoints are disabled (`503`),
but DNS filtering and the block page still work.

The version (`[Site-blocker]` in the repository's `Version.toml`, the only place SMP's
versions are written) is stamped in by the `-ldflags` above; it appears in the startup
log and in `/api/blocklist/status`. That `go build` line expects `Version.toml` and
`tools/version.sh` one directory up (upload them too); a plain `go build .` works as
well and reports `dev`. The Docker image does this itself (its build context is the
repository root).

The last command keeps running until you stop it, for example with `Ctrl+C`.
Check the features from a separate terminal.

### Checking DNS

Blocked domain:

```bash
dig @127.0.0.1 -p 1053 example-blocked.com A
```

The subdomain is blocked too, because the sample entry has
`includeSubdomains` set to `true`:

```bash
dig @127.0.0.1 -p 1053 www.example-blocked.com A
```

Expected answer for both queries:

```text
example-blocked.com. 60 IN A 127.0.0.1
```

A non-blocked domain is forwarded to the upstream DNS:

```bash
dig @127.0.0.1 -p 1053 example.com A
```

AAAA check:

```bash
dig @127.0.0.1 -p 1053 example-blocked.com AAAA
```

For a blocked `AAAA` query the MVP answers `::1`.

### Checking the block page

```bash
curl -H 'Host: example-blocked.com' http://127.0.0.1:8080/
```

Checking the AI-classified example:

```bash
curl -H 'Host: ai-flagged-example.org' http://127.0.0.1:8080/
```

The admin entry shows the block reason and that an admin added it. The AI entry
also shows its confidence value.

**Language.** The block page exists in Hungarian (`blockpage.html`) and English
(`blockpage.en.html`), with a HU | EN switch in its corner. It opens in the school's
default (SMP -> Site blocker -> "Block page language", admins only; it reaches the
sinkhole as `blockPageLanguage` in the sync payload and is saved with the lists), and a
visitor's choice wins: `?lang=hu` or `?lang=en`.

```bash
curl -H 'Host: example-blocked.com' 'http://127.0.0.1:8080/?lang=en'
```

### Importing a blocklist

The import API accepts `hosts` files, newline-separated domains, and common Adblock
`||domain^` rules. Allow rules are ignored. It only accepts calls with the
`DNS_SYNC_TOKEN` Bearer token, and the uploading user's name must be sent in the
`X-Username` HTTP header. (The SMP web UI does not call this endpoint: the Python
API imports the file and then synchronizes.)

```bash
curl -X POST \
  -H "Authorization: Bearer $DNS_SYNC_TOKEN" \
  -H 'X-Username: kovacs.janos' \
  -F 'hosts=@/tmp/custom-hosts.txt' \
  http://127.0.0.1:8080/api/blocklist/import
```

The `addedBy` field of imported entries:

```text
imported by: kovacs.janos
```

The import:

- skips blank lines and comments,
- strips inline comments,
- lowercases domains and removes any trailing dot,
- deduplicates domains,
- blocks subdomains by default,
- sets the `reason` field to `"imported"`,
- saves the imported state to `blocklist.json` immediately.

### Adding a blocklist entry manually

Manual additions also require the Bearer token and the user's name:

```bash
curl -X POST \
  -H "Authorization: Bearer $DNS_SYNC_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'X-Username: kovacs.janos' \
  -d '{
    "domain": "manual-blocked.example",
    "includeSubdomains": true,
    "reason": "Tanórai zavaró tartalom"
  }' \
  http://127.0.0.1:8080/api/blocklist/entries
```

For manual additions, `addedBy` is the frontend user's name as is:

```text
kovacs.janos
```

The system does not accept a client-sent `addedBy` value. Import is the only
exception: there the importing user's name gets the
`"imported by: <username>"` format.

### Configuration

#### `blocklist.json`

Fields of a blocklist entry:

```json
{
  "domain": "example-blocked.com",
  "includeSubdomains": true,
  "reason": "Tanórai zavaró tartalom",
  "addedBy": "admin",
  "confidence": 0.94,
  "addedAt": "2026-11-21T20:00:00Z"
}
```

- `domain`: required; lowercase domain without a trailing dot.
- `includeSubdomains`: if `true`, the base domain and all its subdomains are blocked.
- `reason`: the block reason shown to the user.
- `addedBy`: for example `admin`, `teacher` or `ai`.
- `confidence`: optional; used for entries added by AI.
- `addedAt`: RFC3339 timestamp.

The `reason` values in the samples are in Hungarian because the block page shows
them to students as they are: *Tanórai zavaró tartalom* means "content that is
distracting during lessons", and *Automatikus tartalomminősítés alapján
blokkolva* (in `blocklist.json`) means "blocked by automatic content
classification".

#### `Corefile`

```text
.:1053 {
    schoolfilter ./blocklist.json 127.0.0.1 :8080
    log
    forward . 1.1.1.1
}
```

The `schoolfilter` plugin runs before `log` and `forward`. It answers blocked
queries itself; all other queries are passed on.

### Testing

Run the tests with:

```bash
cd ~/school-dns-demo
go test ./...
```

The tests cover exact and subdomain matching, default private-DNS blocks, empty
HTTPS/SVCB responses for blocked domains, common import formats, and API
authentication. The resolution benchmark compares lookups in 1,000-entry and
100,000-entry blocklists:

```bash
go test ./plugin/schoolfilter -run '^$' -bench '^BenchmarkBlocklistMatch$' -benchmem
```

### Common errors

```text
package school-dns-demo/plugin/schoolfilter: no such directory
```

The `plugin/schoolfilter` directory was not uploaded to the VM. Check:

```bash
ls -la ~/school-dns-demo/plugin/schoolfilter
```

```text
dig: communications error to 127.0.0.1#1053: timed out
```

CoreDNS is not running. Check the `./school-dns-demo` process, or restart it
with:

```bash
cd ~/school-dns-demo
go build .
./school-dns-demo
```

```text
Failed to connect to 127.0.0.1 port 8080
```

The HTTP block page did not start, or no CoreDNS instance is running. Check the
server's console output, then restart it.

---

> Sections 1–8 below describe the **target design** of the DNS service. The
> current MVP above implements a subset of it: blocklist matching, the block
> page, and control-plane sync over HTTP (not NATS yet).

## 1. Overview and Goals

The **DNS Sinkhole Service** is the system's network filtering and routing layer. Its job is to serve DNS requests from the school network quickly and with low latency, enforce classroom/device-based policies, and send unknown domains for asynchronous analysis.

### Principles
* **Zero-dependency runtime (fault tolerance):** The service must keep working fully even when the central control plane (*Core API*, *Postgres*, *NATS*) is unreachable or restarting.
* **Hot path / cold path separation:** DNS response time (hot path) must not depend on external database queries or content analysis. Every slow operation runs asynchronously in the background (cold path).
* **Context-based filtering:** Filtering is not global; it follows the **Device $\rightarrow$ Room $\rightarrow$ Policy** chain identified from the source IP address.

---

## 2. System architecture

```text
                       ┌─────────────────────────────────────────┐
                       │           CORE / CONTROL PLANE          │
                       │  (Core API, Postgres, Web UI, NATS)     │
                       └────────────────────┬────────────────────┘
                                            │ NATS (Pub/Sub & JetStream)
              ┌─────────────────────────────┴─────────────────────────────┐
              ▼ (Policy & IP Sync)                                        ▲ (Logs & New Domains)
┌─────────────────────────────────────────────────────────────────────────┴─────────┐
│                             DNS SINKHOLE CONTAINER (Go)                           │
│                                                                                   │
│  ┌─────────────────────┐      Atomic Swap       ┌──────────────────────────────┐  │
│  │  State Syncer &     │ ─────────────────────► │     IN-MEMORY DNS STATE      │  │
│  │  Local Disk Cache   │                        │  - ClientMap (IP -> Policy)  │  │
│  │  (/data/state.bin)  │                        │  - Reverse Radix Trie        │  │
│  └─────────────────────┘                        │  - Local DNS Overrides       │  │
│                                                 └──────────────┬───────────────┘  │
│                                                                │ $O(1) / O(k)$    │
│  ┌─────────────────────────────────────────────────────────────▼───────────────┐  │
│  │                        DNS ENGINE (miekg/dns - Port 53)                     │  │
│  │   1. Client Lookup  ->  2. SafeSearch/Local  ->  3. Policy & Trie Check     │  │
│  │   4. DNS Response Cache (LRU)  ->  5. Upstream Forwarder (DoT/DoH)          │  │
│  └─────────────────────────────────────────────────────────────┬───────────────┘  │
│                                                                │ Non-blocking     │
│                                                                ▼ Go Channels      │
│                                                 ┌──────────────────────────────┐  │
│                                                 │  Async Telemetry & Event     │  │
│                                                 │  Batcher (Drop-on-full)      │  │
│                                                 └──────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────────────────┘
                                            ▲
                                            │ UDP/TCP Port 53 (Host / Macvlan)
                                 ┌──────────┴──────────┐
                                 │   School clients    │
                                 │ (Classrooms, Wi-Fi) │
                                 └─────────────────────┘
```

---

## 3. Data model and memory layout (hot path)

The whole filtering state lives in memory and is read lock-free. On an update the new state is built in the background, then takes effect through an `atomic.Value` pointer swap.

### Go data structures

```go
package model

import "net"

// DNSState is the complete, atomically swappable in-memory state
type DNSState struct {
    Version     int64
    // IPv4 (uint32) -> PolicyID mapping for fast lookup
    ClientMap   map[uint32]string
    // PolicyID -> policy definition
    Policies    map[string]*Policy
    // Reversed domain names (e.g. "com.tiktok") -> CategoryID
    DomainTrie  *RadixTrie
    // Internal network records (e.g. "go.school." -> 10.10.0.1)
    LocalRecords map[string]net.IP
}

type PolicyMode string

const (
    ModeBlacklist PolicyMode = "blacklist" // Normal mode: everything allowed except what is blocked
    ModeWhitelist PolicyMode = "whitelist" // Exam mode: everything blocked except the exceptions
)

type Policy struct {
    ID                string
    Name              string
    Mode              PolicyMode
    BlockedCategories map[uint16]bool // Blocked category IDs (e.g. 1: Adult, 2: Games, 3: Social)
    CustomBlock       map[string]bool // Individually blocked domains (e.g. blocked by a teacher)
    CustomAllow       map[string]bool // Individually allowed domains (also used in exam mode)
    ForceSafeSearch   bool            // Enforce Google, Bing, YouTube SafeSearch
}
```

### Why these data structures
* **Reverse radix trie:** Domains are stored reversed (`www.sub.example.com` $\rightarrow$ `com.example.sub.www`). This gives $O(k)$ lookup (where $k$ is the domain length) and native wildcard support (`*.tiktok.com`) without wasting memory.
* **Persistence:** Every successful state update is written to disk asynchronously (`/data/dns_state.bin`, compressed binary format). After a container restart the service starts from this file within 100 ms, without network access.

---

## 4. Lifecycle of a DNS request (processing steps)

When a client sends a DNS query to port 53, the engine decides in this order:

1. **Identify the client:** Looks up the `PolicyID` for the source IP in `ClientMap`. Unknown IPs get the `default_policy`.
2. **Internal records (local DNS):** Checks the `LocalRecords` table (e.g. `go.school` $\rightarrow$ internal portal IP). On a match it answers immediately.
3. **SafeSearch rewrite:** If `Policy.ForceSafeSearch == true`, for search engine and video sharing domains (e.g. `www.google.com`, `www.youtube.com`) it returns a CNAME/A record pointing to the providers' official SafeSearch IP addresses (e.g. `forcesafesearch.google.com`).
4. **Exception lists (overrides):**
   * If the domain is on the `Policy.CustomAllow` list $\rightarrow$ **ALLOWED** (go to step 7).
   * If the domain is on the `Policy.CustomBlock` list $\rightarrow$ **BLOCKED** (go to step 6).
5. **Mode and category check:**
   * **If `Mode == "whitelist"` (exam mode):** since step 4 found no match on the `CustomAllow` list $\rightarrow$ **BLOCKED**.
   * **If `Mode == "blacklist"`:** looks the domain up in `DomainTrie`. If it has a category and that category is in the `Policy.BlockedCategories` set $\rightarrow$ **BLOCKED**.
6. **Blocked response (sinkhole):**
   * For `A` / `AAAA` queries it returns the internal IP address of the block page (e.g. `10.10.0.2`, where a lightweight HTTP server tells the student why the site is blocked) or `0.0.0.0` (to avoid `NXDOMAIN` HTTPS errors).
7. **Resolution and cache (allowed request):**
   * Checks the in-memory DNS response cache (respecting TTLs).
   * On a cache miss it forwards the query to the upstream DNS server (e.g. Cloudflare `1.1.1.2` or Quad9 `9.9.9.9` over DNS-over-TLS).
8. **Asynchronous logging:** Puts the request metadata (timestamp, client IP, domain, category, status: `ALLOWED`/`BLOCKED`) into a buffered Go channel. If the channel is full (under extreme load), the log entry is dropped so DNS response times don't suffer.

---

## 5. Classification pipeline (cold path)

Unknown domains are classified in a separate worker process, so classification doesn't slow down DNS resolution.

```text
[New domain detected] ──► (NATS: dns.domain.unclassified)
                                  │
                                  ▼
                 ┌─────────────────────────────────┐
                 │  STAGE 1: Deterministic         │
                 │  - UT1 / Toulouse lists         │
                 │  - Hagezi & StevenBlack lists   │
                 │  - DoH / proxy detection        │
                 │  - Regex & DGA heuristics       │
                 └────────────────┬────────────────┘
                                  │ Still unknown
                                  ▼
                 ┌─────────────────────────────────┐
                 │  STAGE 2: Content analysis (LLM)│
                 │  - HTTP GET (3s timeout, sandbox)│
                 │  - Extract <title> and <meta>   │
                 │  - Local/API LLM classification │
                 └────────────────┬────────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 ▼ (Confidence > 0.90)             ▼ (Confidence 0.50 - 0.90)
        [Automatic block]                  [Review Queue (Web UI)]
        NATS: blocklist.updated            Awaits teacher/admin approval
```

### Details
* **Deduplication:** The DNS server keeps an in-memory LRU cache / Bloom filter of domains it has already seen, so each new domain is sent to the NATS queue only once.
* **Allow on first sight:** A completely new, not yet categorized domain is allowed on its first request (except in *exam mode*), while its classification starts in the background.

---

## 6. NATS communication contract (event subjects)

| Subject | Direction | Description | Payload example |
| :--- | :--- | :--- | :--- |
| `dns.policy.sync` | Core $\rightarrow$ DNS | Full or incremental policy update | `{"version": 142, "policies": [...]}` |
| `dns.client.updated` | Core $\rightarrow$ DNS | A device's IP address or room changed | `{"ip": "10.10.2.45", "policy_id": "exam_1"}` |
| `dns.blocklist.updated` | Worker $\rightarrow$ DNS | Add a newly classified domain to the trie | `{"domain": "newgame.io", "category_id": 2}` |
| `dns.domain.unclassified`| DNS $\rightarrow$ Worker| Send a not-yet-seen domain for analysis | `{"domain": "unknown-site.hu", "org_id": 1}` |
| `dns.logs.batch` | DNS $\rightarrow$ Worker| Batched DNS log entries (1 s / 1000 entries) | `[{"ts": 1732190000, "ip": "...", "q": "..."}]` |

---

## 7. Security and anti-bypass protection

In a school environment students actively try to get around filtering. The service uses these protections:

1. **Blocking DoH (DNS-over-HTTPS):**
   * The sinkhole blocks common public DoH endpoints and Apple's iCloud Private Relay
     endpoints by default (`dns.google`, `cloudflare-dns.com`, `dns.quad9.net`,
     `mask.icloud.com`, `mask-h2.icloud.com`, and others). An explicit allowlist entry
     can override these defaults.
   * Blocked HTTPS (SVCB/HTTPS) DNS queries receive an authoritative empty `NOERROR`
     answer, so HTTPS records cannot advertise an alternate endpoint.
   * To stop Firefox/Chrome from switching to DoH automatically, the server answers `NXDOMAIN` for `use-application-dns.net`.
2. **Firewall rules (documented requirement):**
   * The school gateway (router/firewall) must block all outbound **UDP/TCP 53** and **TCP 853 (DoT)** traffic, except from the DNS sinkhole server itself.
3. **Client-side hardening:**
   * The Windows agent disables the browsers' built-in Secure DNS feature and changes to proxy settings through GPO / the registry.

---

## 8. Operations and deployment

The service is a single static Go binary packaged in a Docker container.

### Docker Compose configuration

```yaml
services:
  dns-sinkhole:
    image: schoolguard/dns-sinkhole:latest
    container_name: sg-dns
    restart: always
    # Host networking or macvlan is needed so the container
    # sees the clients' real source IP addresses (no NAT)
    network_mode: host
    environment:
      - NATS_URL=nats://127.0.0.1:4222
      - UPSTREAM_DNS=1.1.1.2:853
      - UPSTREAM_PROTO=dot
      - STATE_FILE=/data/dns_state.bin
      - SINKHOLE_IPV4=10.10.0.2
      - LOG_LEVEL=info
    volumes:
      - ./data/dns:/data
    cap_add:
      - NET_BIND_SERVICE
```

### Metrics and monitoring (Prometheus)
The service exposes a `/metrics` endpoint on port `9153` with these metrics:
* `dns_queries_total{status="allowed|blocked|cached", policy_id="..."}`
* `dns_query_duration_seconds` (histogram of response times)
* `dns_trie_domains_count` (number of loaded domains)
* `dns_dropped_logs_total` (number of logs dropped due to overload)
