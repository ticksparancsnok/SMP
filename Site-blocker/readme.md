# DNS Sinkhole & Tartalomszűrő Szolgáltatás (DNS Resolver Service)

## Helyi MVP futtatása

A `school-dns-demo` könyvtár tartalmazza a helyi DNS sinkhole és blokkoló oldal
demót. A szolgáltatás két protokollon érhető el:

| Szolgáltatás | Alapértelmezett cím |
|---|---|
| CoreDNS DNS szolgáltatás | `127.0.0.1:1053` |
| HTTP blokkoló oldal | `127.0.0.1:8080` |

A `schoolfilter` plugin a `blocklist.json` fájlt indításkor beolvassa és
memóriában tárolja. A DNS hot path ezért nem végez fájlrendszer- vagy
vezérlősík-lekérdezést.

## SMP API és webes kezelés integrációja

The Go sinkhole also accepts control-plane synchronization:

```text
POST /api/blocklist/sync
Authorization: Bearer <DNS_SYNC_TOKEN>
```

The SMP Python API stores entries in PostgreSQL and synchronizes the complete
canonical `entries` array after manual changes and hosts imports. The Go service
replaces its in-memory state and writes `blocklist.json`.

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
block page to every client on the school network, so the API must not be open.
If `DNS_SYNC_TOKEN` is not set, the API answers `503` (disabled, not open);
DNS filtering and the block page keep working.

Docker Compose configuration uses `SMP_DNS_SINKHOLE_URL`,
`SMP_DNS_SYNC_TOKEN`, and the Go container's `DNS_SYNC_TOKEN`. Compose refuses
to start without `SMP_DNS_SYNC_TOKEN` and passes the same value to both sides.

### Telepítés az IntelliJ SFTP deploymenttel

Az IntelliJ Deployment konfigurációjának célhelye legyen:

```text
/home/vboxuser/school-dns-demo
```

A következő helyi projektfájloknak és könyvtáraknak kell a VM-re felkerülniük:

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
        ├── blockpage.html
        ├── hostsimport.go
        ├── schoolfilter.go
        └── schoolfilter_test.go
```

IntelliJ-ben használd a **Deployment → Upload to Debian** műveletet. Az
**Excluded Paths** beállításokban ne legyen kizárva a `plugin` vagy a
`schoolfilter` könyvtár.

### Fordítás és futtatás Debian VM-en

```bash
cd ~/school-dns-demo
go mod tidy
go test ./...
go build .
export DNS_SYNC_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
./school-dns-demo
```

A `DNS_SYNC_TOKEN` nélkül a `/api/blocklist/*` végpontok le vannak tiltva
(`503`), a DNS szűrés és a blokkoló oldal viszont működik.

Az utolsó parancs addig fut, amíg meg nem szakítod például `Ctrl+C`-vel.
Egy külön terminálból ellenőrizhetők a funkciók.

### DNS ellenőrzés

Blokkolt domain:

```bash
dig @127.0.0.1 -p 1053 example-blocked.com A
```

Az aldomain is blokkolva van, mert a minta bejegyzésben az
`includeSubdomains` értéke `true`:

```bash
dig @127.0.0.1 -p 1053 www.example-blocked.com A
```

Mindkét lekérdezésre várható válasz:

```text
example-blocked.com. 60 IN A 127.0.0.1
```

Nem blokkolt domain továbbítása az upstream DNS felé:

```bash
dig @127.0.0.1 -p 1053 example.com A
```

AAAA ellenőrzés:

```bash
dig @127.0.0.1 -p 1053 example-blocked.com AAAA
```

Az MVP blokkolt `AAAA` lekérdezésre `::1` címet ad vissza.

### Blokkoló oldal ellenőrzése

```bash
curl -H 'Host: example-blocked.com' http://127.0.0.1:8080/
```

AI besorolású példa ellenőrzése:

```bash
curl -H 'Host: ai-flagged-example.org' http://127.0.0.1:8080/
```

Az admin bejegyzés a tiltás okát és azt jeleníti meg, hogy az admin adta hozzá.
Az AI bejegyzés a bizonyossági értékét is megjeleníti.

### Hosts blokklista importálása

Az importálási API a feltöltött `hosts` fájlt fogadja. A hívás csak
`DNS_SYNC_TOKEN` Bearer tokennel engedélyezett; a feltöltő felhasználó neve
kötelezően `X-Username` HTTP fejléccel érkezik. (Az SMP webes felülete nem ezt a
végpontot hívja: a Python API importál, majd szinkronizál.)

```bash
curl -X POST \
  -H "Authorization: Bearer $DNS_SYNC_TOKEN" \
  -H 'X-Username: kovacs.janos' \
  -F 'hosts=@/tmp/custom-hosts.txt' \
  http://127.0.0.1:8080/api/blocklist/import
```

Az importált bejegyzések `addedBy` mezője:

```text
imported by: kovacs.janos
```

Az importálás:

- kihagyja az üres sorokat és kommenteket,
- levágja az inline megjegyzéseket,
- kisbetűsíti és pont nélküli formára normalizálja a domaineket,
- deduplikálja a domaint,
- az aldomainokat alapértelmezetten blokkolja,
- az `reason` mezőt `"imported"` értékre állítja,
- az importált állapotot azonnal menti a `blocklist.json` fájlba.

### Kézi blokklista-hozzáadás

A kézi művelethez is kötelező a Bearer token és a felhasználó neve:

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

A kézi hozzáadásnál az `addedBy` értéke közvetlenül a frontend felhasználó neve:

```text
kovacs.janos
```

A rendszer nem fogad el kliens által küldött `addedBy` értéket. Az importálás
az egyetlen kivétel, ott az importálási felhasználónév az
`"imported by: <username>"` formátumot kapja.

### Konfiguráció

#### `blocklist.json`

Egy blokklista-bejegyzés mezői:

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

- `domain`: kötelező, kisbetűs domain, záró pont nélkül.
- `includeSubdomains`: `true` esetén az alapdomain és az összes aldomain blokkolva van.
- `reason`: megjelenő tiltási indok.
- `addedBy`: például `admin`, `teacher` vagy `ai`.
- `confidence`: opcionális érték; AI által hozzáadott bejegyzéseknél használható.
- `addedAt`: RFC3339 formátumú időbélyeg.

#### `Corefile`

```text
.:1053 {
    schoolfilter ./blocklist.json 127.0.0.1 :8080
    log
    forward . 1.1.1.1
}
```

A `schoolfilter` plugin sorrendben a `log` és `forward` előtt fut. A blokkolt
lekérdezéseket ő válaszolja meg; a többi lekérdezés továbbítódik.

### Tesztelés

A következő teszteket futtatja:

```bash
cd ~/school-dns-demo
go test ./...
```

A tesztek fedik az exact és aldomain matchinget, a záró pont nélküli és
nagybetűs domaineket, az AI confidence mező JSON-ből való betöltését,
valamint azt, hogy az `/api/blocklist/*` végpontok token nélkül, hibás tokennel
vagy beállítatlan `DNS_SYNC_TOKEN` mellett elutasítják a kérést, a blokkoló
oldal pedig nyilvános marad.

### Gyakori hibák

```text
package school-dns-demo/plugin/schoolfilter: no such directory
```

Ez akkor jelentkezik, amikor a `plugin/schoolfilter` könyvtár nem került fel
a VM-re. Ellenőrizd:

```bash
ls -la ~/school-dns-demo/plugin/schoolfilter
```

```text
dig: communications error to 127.0.0.1#1053: timed out
```

Ez azt jelenti, hogy a CoreDNS nincs futásban. Ellenőrizd a
`./school-dns-demo` processzt, vagy indítsd újra a következő parancsokkal:

```bash
cd ~/school-dns-demo
go build .
./school-dns-demo
```

```text
Failed to connect to 127.0.0.1 port 8080
```

Ez azt jelenti, hogy a HTTP blokkoldal nem indult el vagy nincs futó
CoreDNS-példány. Ellenőrizd a szerver konzolkimenetét, majd indítsd újra.

## 1. Áttekintés és Célok

A **DNS Sinkhole Service** a rendszer hálózati szűrő- és irányítórétege. Feladata az iskolai hálózatból érkező DNS kérések nagy sebességű, alacsony késleltetésű kiszolgálása, a tantermi/eszközalapú szabályzatok (Policy) érvényesítése, valamint az ismeretlen domainek aszinkron elemzésre küldése.

### Alapelvek
* **Zero-Dependency Runtime (Hibatűrés):** A szolgáltatásnak teljes értékűen működnie kell akkor is, ha a központi vezérlősík (*Core API*, *Postgres*, *NATS*) elérhetetlen vagy újraindul.
* **Hot Path / Cold Path szétválasztása:** A DNS válaszidő (Hot Path) nem függhet külső adatbázis-lekérdezésektől vagy tartalomelemző algoritmusoktól. Minden lassú művelet aszinkron módon, a háttérben (Cold Path) fut.
* **Kontextus-alapú szűrés:** A szűrés nem globális, hanem a forrás IP-cím alapján azonosított **Eszköz (Device) $\rightarrow$ Tanterem (Room) $\rightarrow$ Szabályzat (Policy)** láncon alapul.

---

## 2. Rendszerarchitektúra

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
                                 │  Iskolai Kliensek   │
                                 │  (Tantermek, Wi-Fi) │
                                 └─────────────────────┘
```

---

## 3. Adatmodell és Memória-struktúra (Hot Path)

A teljes szűrési állapot a memóriában él, zárolásmentes (lock-free) olvasással. Frissítéskor a háttérben épül fel az új állapot, majd egy `atomic.Value` pointer cserével lép életbe.

### Go Adatstruktúrák

```go
package model

import "net"

// DNSState a teljes, atomikusan cserélhető memóriabeli állapot
type DNSState struct {
    Version     int64
    // IPv4 (uint32) -> PolicyID leképezés a gyors kereséshez
    ClientMap   map[uint32]string
    // PolicyID -> Szabályzat definíció
    Policies    map[string]*Policy
    // Megfordított domain nevek (pl. "com.tiktok") -> CategoryID
    DomainTrie  *RadixTrie
    // Belső hálózati rekordok (pl. "go.school." -> 10.10.0.1)
    LocalRecords map[string]net.IP
}

type PolicyMode string

const (
    ModeBlacklist PolicyMode = "blacklist" // Normál üzemmód: minden engedélyezve, kivéve a tiltottak
    ModeWhitelist PolicyMode = "whitelist" // Vizsga mód (Exam Mode): minden tiltva, kivéve a kivételek
)

type Policy struct {
    ID                string
    Name              string
    Mode              PolicyMode
    BlockedCategories map[uint16]bool // Tiltott kategória ID-k (pl. 1: Adult, 2: Games, 3: Social)
    CustomBlock       map[string]bool // Egyedileg tiltott domainek (pl. tanár által blokkolva)
    CustomAllow       map[string]bool // Egyedileg engedélyezett domainek (vizsga módhoz is)
    ForceSafeSearch   bool            // Google, Bing, YouTube SafeSearch kényszerítése
}
```

### Adatszerkezetek indoklása
* **Reverse Radix Trie:** A domainek megfordítva kerülnek tárolásra (`www.sub.example.com` $\rightarrow$ `com.example.sub.www`). Ez lehetővé teszi az $O(k)$ (ahol $k$ a domain hossza) idejű keresést és a natív wildcard támogatást (`*.tiktok.com`) memóriapazarlás nélkül.
* **Perzisztencia:** Minden sikeres állapotfrissítés aszinkron módon kiíródik a lemezre (`/data/dns_state.bin` tömörített bináris formátumban). Konténer újraindításkor a szolgáltatás ebből a fájlból indul el 100 ms alatt, hálózati kapcsolat nélkül.

---

## 4. A DNS Kérés Életciklusa (Feldolgozási Lépések)

Amikor egy kliens DNS lekérdezést küld a 53-as portra, a motor az alábbi sorrendben dönt:

1. **Kliens azonosítása:** A forrás IP-cím alapján kikeresi a `PolicyID`-t a `ClientMap`-ből. Ismeretlen IP esetén a `default_policy` lép életbe.
2. **Belső rekordok (Local DNS):** Ellenőrzi a `LocalRecords` táblát (pl. `go.school` $\rightarrow$ belső portál IP). Találat esetén azonnal válaszol.
3. **SafeSearch átírás:** Ha a `Policy.ForceSafeSearch == true`, a keresőmotorok és videómegosztók domainjeire (pl. `www.google.com`, `www.youtube.com`) CNAME/A rekordot ad vissza a szolgáltatók hivatalos SafeSearch IP-címeire (pl. `forcesafesearch.google.com`).
4. **Kivétellisták (Overrides):**
   * Ha a domain szerepel a `Policy.CustomAllow` listán $\rightarrow$ **ENGEDÉLYEZVE** (ugrás a 7. lépésre).
   * Ha a domain szerepel a `Policy.CustomBlock` listán $\rightarrow$ **BLOKKOLVA** (ugrás a 6. lépésre).
5. **Üzemmód és Kategória ellenőrzés:**
   * **Ha `Mode == "whitelist"` (Exam Mode):** Mivel a 4. lépésben nem volt találat a `CustomAllow` listán $\rightarrow$ **BLOKKOLVA**.
   * **Ha `Mode == "blacklist"`:** Kikeresi a domaint a `DomainTrie`-ból. Ha van kategória találat, és az szerepel a `Policy.BlockedCategories` halmazban $\rightarrow$ **BLOKKOLVA**.
6. **Blokkolt válasz (Sinkhole):**
   * `A` / `AAAA` rekord kérés esetén visszatér a blokkoló oldal belső IP-címével (pl. `10.10.0.2`, ahol egy könnyűsúlyú HTTP szerver közli a diákkal a tiltás okát) vagy `0.0.0.0`-val (`NXDOMAIN` HTTPS hibák elkerülése végett).
7. **Feloldás és Cache (Engedélyezett kérés):**
   * Ellenőrzi a memóriabeli DNS válasz-cache-t (TTL figyelembevételével).
   * Cache miss esetén továbbítja a kérést az upstream DNS szervernek (pl. Cloudflare `1.1.1.2` vagy Quad9 `9.9.9.9` DNS-over-TLS kapcsolaton keresztül).
8. **Aszinkron naplózás:** A kérés metaadatait (Időbélyeg, Kliens IP, Domain, Kategória, Státusz: `ALLOWED`/`BLOCKED`) beteszi egy pufferelt Go channelbe. Ha a csatorna megtelik (extrém terhelésnél), a logot eldobja, hogy a DNS válaszidő ne romoljon.

---

## 5. Klasszifikációs Pipeline (Cold Path)

Az ismeretlen domainek besorolása külön worker folyamatban történik, így nem terheli a DNS feloldást.

```text
[Új domain észlelése] ──► (NATS: dns.domain.unclassified)
                                  │
                                  ▼
                 ┌─────────────────────────────────┐
                 │  STAGE 1: Determinisztikus      │
                 │  - UT1 / Toulouse listák        │
                 │  - Hagezi & StevenBlack listák  │
                 │  - DoH / Proxy detektálás       │
                 │  - Regex & DGA heurisztika      │
                 └────────────────┬────────────────┘
                                  │ Ismeretlen maradt
                                  ▼
                 ┌─────────────────────────────────┐
                 │  STAGE 2: Tartalomelemzés (LLM) │
                 │  - HTTP GET (3s timeout, sandbox)│
                 │  - <title> és <meta> kinyerése  │
                 │  - Helyi/API LLM osztályozás    │
                 └────────────────┬────────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 ▼ (Confidence > 0.90)             ▼ (Confidence 0.50 - 0.90)
        [Automatikus Tiltás]               [Review Queue (Web UI)]
        NATS: blocklist.updated            Tanár/Admin jóváhagyásra vár
```

### Részletek
* **Deduplikáció:** A DNS szerver egy memóriabeli LRU cache-t / Bloom filtert tart fenn a már látott domainekről, így egy új domaint csak egyszer küld be a NATS sorba.
* **Első megtekintés elve (Allow on first sight):** Egy teljesen új, még nem kategorizált domain az első kéréskor átengedésre kerül (kivéve *Exam Mode*-ban), miközben a háttérben elindul a besorolása.

---

## 6. NATS Kommunikációs Szerződés (Event Subjects)

| Téma (Subject) | Irány | Leírás | Payload példa |
| :--- | :--- | :--- | :--- |
| `dns.policy.sync` | Core $\rightarrow$ DNS | Teljes vagy inkrementális szabályzat-frissítés | `{"version": 142, "policies": [...]}` |
| `dns.client.updated` | Core $\rightarrow$ DNS | Eszköz IP-címének vagy tantermének változása | `{"ip": "10.10.2.45", "policy_id": "exam_1"}` |
| `dns.blocklist.updated` | Worker $\rightarrow$ DNS | Új besorolt domain hozzáadása a Trie-hoz | `{"domain": "newgame.io", "category_id": 2}` |
| `dns.domain.unclassified`| DNS $\rightarrow$ Worker| Még nem látott domain küldése elemzésre | `{"domain": "unknown-site.hu", "org_id": 1}` |
| `dns.logs.batch` | DNS $\rightarrow$ Worker| Kötegelt DNS naplóbejegyzések (1 mp / 1000 db) | `[{"ts": 1732190000, "ip": "...", "q": "..."}]` |

---

## 7. Biztonság és Megkerülés elleni védelem (Anti-Bypass)

Iskolai környezetben a diákok aktívan próbálják megkerülni a szűrést. A szolgáltatás az alábbi védelmi mechanizmusokat alkalmazza:

1. **DoH (DNS-over-HTTPS) blokkolása:**
   * A beépített tiltólisták tartalmazzák az összes ismert publikus DoH/DoT szerver domainjét (pl. `dns.google`, `cloudflare-dns.com`, `dns.quad9.net`).
   * A Firefox/Chrome automatikus DoH átállásának tiltására a `use-application-dns.net` kérésre a szerver `NXDOMAIN` választ ad.
2. **Tűzfal szabályok (Dokumentált követelmény):**
   * Az iskolai átjárón (router/firewall) blokkolni kell minden kimenő **UDP/TCP 53** és **TCP 853 (DoT)** forgalmat, kivéve magát a DNS Sinkhole szervert.
3. **Kliens oldali megerősítés:**
   * A Windows Agent GPO / Registry szinten tiltja a böngészők beépített Secure DNS funkcióját és a proxy beállítások módosítását.

---

## 8. Üzemeltetés és Telepítés (Deployment)

A szolgáltatás egyetlen statikus Go binárisból áll, Docker konténerbe csomagolva.

### Docker Compose konfiguráció

```yaml
services:
  dns-sinkhole:
    image: schoolguard/dns-sinkhole:latest
    container_name: sg-dns
    restart: always
    # Host networking vagy macvlan szükséges, hogy a konténer 
    # lássa a kliensek valódi forrás IP-címét (NAT nélkül)
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

### Metrikák és Monitorozás (Prometheus)
A szolgáltatás a `9153`-as porton `/metrics` végpontot biztosít az alábbi mutatókkal:
* `dns_queries_total{status="allowed|blocked|cached", policy_id="..."}`
* `dns_query_duration_seconds` (hisztogram a válaszidő mérésére)
* `dns_trie_domains_count` (betöltött domainek száma)
* `dns_dropped_logs_total` (túlterhelés miatt eldobott naplók száma)
