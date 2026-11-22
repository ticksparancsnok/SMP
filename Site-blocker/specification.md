# Hosts blokklista API-n keresztüli importálása a cél-JSON sémába

## Áttekintés

A rendszerbe érkező `hosts` formátumú blokklista-fájlok a **frontend felől API-n keresztül** érkeznek be a backendre, és közvetlenül a rendszer saját JSON formátumába (`entries` tömb a megadott mezőkkel) kerülnek átalakításra és beimportálásra.

## Bemeneti folyamat (Frontend → API → Cél-JSON)

1. **Feltöltés a frontendről:** A felhasználó a frontend felületen kiválasztja és feltölti a `hosts` fájlt az importáló API-végpontra.

2. **Felhasználó és időbélyeg meghatározása:** A backend azonosítja a feltöltő felhasználót (`[username]`), és rögzíti az importálás időpontját ISO-8601 UTC formátumban (`addedAt`, pl. `2026-11-21T20:00:00Z`).

3. **Feldolgozás és normalizálás:** A backend kiolvassa a blokkolandó domaineket a `hosts` fájlból, kiszűri a nem blokkolandó rendszer-/loopback bejegyzéseket, normalizálja a domainneveket, és deduplikálja a listát.

4. **Beimportálás a cél-JSON struktúrába:** Minden kinyert domainből egy-egy objektum jön létre az alábbi séma szerint az `entries` tömbben.

## Cél-JSON séma

```json
{
  "entries": [
    {
      "domain": "example-blocked.com",
      "includeSubdomains": true,
      "reason": "imported",
      "addedBy": "imported by: [username]",
      "addedAt": "2026-11-21T20:00:00Z"
    }
  ]
}
```

### Mezők leírása

* **`entries`** (`array<object>`): A beimportált domain bejegyzések listája.

* **`domain`** (`string`): A kisbetűsített, záró pont (`"."`) nélküli blokkolandó domainnév (pl. `"example-blocked.com"`).

* **`includeSubdomains`** (`boolean`): Alapértelmezetten `true` (hacsak a rendszer/konfiguráció másként nem rendelkezik).

* **`reason`** (`string`): A bejegyzés okát jelölő érték, importálás esetén `"imported"`.

* **`addedBy`** (`string`): A feltöltő felhasználó azonosítója a megadott formátumban: `"imported by: [username]"`.

* **`addedAt`** (`string`): Az importálás időpontja ISO-8601 UTC formátumban (pl. `"2026-11-21T20:00:00Z"`).

## Feldolgozási és normalizálási szabályok

1. **Üres sorok és kommentek:**
   * Az üres és kizárólag whitespace karaktereket tartalmazó sorokat a feldolgozó kihagyja.
   * A `#` karakterrel kezdődő sorok kommentek vagy fejlécek, nem domain bejegyzések.
   * A sorokon belüli `#` utáni megjegyzéseket a host rekord feldolgozása előtt levágjuk.

2. **Domainek kinyerése és normalizálása:**
   * A fennmaradó tokeneket whitespace mentén szétválasztjuk: az első token az IP-cím, a további tokenek a domainnevek.
   * Ha egy sorban több domain szerepel, mindegyik domain külön bejegyzést kap az `entries` tömbben.
   * A `domain` értékét kisbetűsre alakítjuk, és eltávolítjuk a végéről a záró pontot (ha van).

3. **Szűrés és deduplikálás:**
   * Kiszűrjük a nem blokkolandó rendszer-/loopback/multicast/broadcast bejegyzéseket (pl. `::1`, `fe80::...`, `ff00::...`, `ff02::...`, `255.255.255.255`, `localhost`, `broadcasthost`), valamint a `0.0.0.0` / `127.0.0.1` önmagában álló rendszerbejegyzéseket.
   * A `0.0.0.0` vagy `127.0.0.1` után megadott valós domaineket blokkolandóként kezeljük.
   * Az azonos `domain` értékeket deduplikáljuk, hogy ne kerüljenek ismétlődő elemek a cél-JSON-ba.

## LLM-alapú átalakító prompt (teszteléshez / kiegészítő lépéshez)

```markdown
Convert the provided hosts blocklist content into the target JSON format.

### Output Format
- Output **valid JSON only** (no markdown code fences unless requested, no commentary before or after).
- Use this exact structure:
```json
{
  "entries": [
    {
      "domain": "example-blocked.com",
      "includeSubdomains": true,
      "reason": "imported",
      "addedBy": "imported by: [username]",
      "addedAt": "2026-11-21T20:00:00Z"
    }
  ]
}
```
- Replace `[username]` in `"addedBy"` with the uploading user's name/identifier.
- Set `"addedAt"` to the import timestamp in ISO-8601 UTC format (e.g., `"2026-11-21T20:00:00Z"`).

### Rules
1. Ignore blank lines and lines starting with `#`. Strip inline `# ...` comments before parsing.
2. Split remaining tokens by whitespace: skip the IP token and extract the domain token(s). If multiple domains appear on one line, create an entry for each.
3. Normalize each `"domain"` to lowercase and remove any trailing dot.
4. Exclude non-blocking system/loopback/multicast/broadcast entries (`::1`, `fe80::...`, `ff00::...`, `ff02::...`, `255.255.255.255`, `localhost`, `broadcasthost`) and non-domain IP tokens.
5. Deduplicate entries by `"domain"` (keep first occurrence).
6. For each valid unique domain, set `"includeSubdomains": true`, `"reason": "imported"`, `"addedBy": "imported by: [username]"`, and `"addedAt": "<ISO-8601 UTC timestamp>"`.

### Input Data
<PASTE_HOSTS_CONTENT_HERE>
```