# Importing a hosts blocklist through the API into the target JSON schema

## Overview

Blocklist files in `hosts` format reach the backend **from the frontend through the API**, and are converted directly into the system's own JSON format (an `entries` array with the fields below) and imported.

## Input flow (Frontend → API → target JSON)

1. **Upload from the frontend:** The user selects a `hosts` file in the frontend and uploads it to the import API endpoint.

2. **Determine the user and timestamp:** The backend identifies the uploading user (`[username]`) and records the import time in ISO-8601 UTC format (`addedAt`, e.g. `2026-11-21T20:00:00Z`).

3. **Processing and normalization:** The backend reads the domains to block from the `hosts` file, filters out system/loopback entries that should not be blocked, normalizes the domain names, and deduplicates the list.

4. **Import into the target JSON structure:** Each extracted domain becomes one object in the `entries` array, following the schema below.

## Target JSON schema

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

### Fields

* **`entries`** (`array<object>`): The list of imported domain entries.

* **`domain`** (`string`): The domain name to block, lowercased and without a trailing dot (`"."`) (e.g. `"example-blocked.com"`).

* **`includeSubdomains`** (`boolean`): `true` by default (unless the system/configuration says otherwise).

* **`reason`** (`string`): The reason for the entry; `"imported"` for imports.

* **`addedBy`** (`string`): The uploading user's identifier in this format: `"imported by: [username]"`.

* **`addedAt`** (`string`): The import time in ISO-8601 UTC format (e.g. `"2026-11-21T20:00:00Z"`).

## Processing and normalization rules

1. **Blank lines and comments:**
   * Lines that are empty or contain only whitespace are skipped.
   * Lines starting with `#` are comments or headers, not domain entries.
   * Inline comments after `#` are stripped before the host record is processed.

2. **Extracting and normalizing domains:**
   * The remaining tokens are split on whitespace: the first token is the IP address, the following tokens are domain names.
   * If a line contains several domains, each domain gets its own entry in the `entries` array.
   * The `domain` value is lowercased and its trailing dot (if any) is removed.

3. **Filtering and deduplication:**
   * System/loopback/multicast/broadcast entries that should not be blocked are filtered out (e.g. `::1`, `fe80::...`, `ff00::...`, `ff02::...`, `255.255.255.255`, `localhost`, `broadcasthost`), as are standalone `0.0.0.0` / `127.0.0.1` system entries.
   * Real domains listed after `0.0.0.0` or `127.0.0.1` are treated as domains to block.
   * Identical `domain` values are deduplicated so the target JSON contains no repeated items.

## LLM-based conversion prompt (for testing / as a supplementary step)

````markdown
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
````
