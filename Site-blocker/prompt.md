# School DNS Sinkhole MVP — Agent Prompt

Build an MVP DNS sinkhole and custom block-page service in Go on top of the existing CoreDNS wrapper project (`school-dns-demo`, module `school-dns-demo`, Go 1.27+, using `github.com/coredns/coredns` v1.15.0).

## Context & Existing Setup
- We already have a working Go wrapper around CoreDNS (`main.go` calling `coremain.Run()`, `go.mod`, `go.sum`, and a working `Corefile` on port `1053` forwarding to `1.1.1.1`).
- The target environment is a Debian Linux VM (where `go build .` compiles the binary and runs tests), edited from Windows via IntelliJ IDEA over SFTP (`~/school-dns-demo`).

## Goal for the MVP
Create a working local demo of a school network DNS sinkhole that:
1. Loads blocked domains from a JSON file (`blocklist.json`).
2. Intercepts DNS queries in a custom CoreDNS plugin (`schoolfilter`) placed *before* `cache` and `forward` in the `Corefile`.
3. When a domain matches the blocklist (respecting `includeSubdomains`), returns a sinkhole IP address (configurable via plugin argument, e.g., `127.0.0.1` for local demo). Non-blocked domains pass through to the next plugin (`forward` / `cache`).
4. Runs a simple HTTP server (e.g., on `:8080`) that serves a custom HTML block page (`blockpage.html`) when a user visits a blocked domain resolving to the sinkhole IP, displaying information such as the requested domain, reason, and `addedBy` (including `confidence` when added by AI).

## JSON Blocklist Schema (`blocklist.json`)
Keep the schema clean and compatible with future admin UI and AI classification:

```json
{
  "entries": [
    {
      "domain": "example-blocked.com",
      "includeSubdomains": true,
      "reason": "Tanórai zavaró tartalom",
      "addedBy": "admin",
      "addedAt": "2026-11-21T20:00:00Z"
    },
    {
      "domain": "ai-flagged-example.org",
      "includeSubdomains": true,
      "reason": "Automatikus tartalomminősítés alapján blokkolva",
      "addedBy": "ai",
      "confidence": 0.94,
      "addedAt": "2026-11-21T20:05:00Z"
    }
  ]
}
```

- `domain` (string, required): normalized lowercase domain without trailing dot.
- `includeSubdomains` (bool): if true, matches both `domain` and any `*.<domain>`; if false, exact match only.
- `reason` (string, optional): human-readable reason.
- `addedBy` (string): e.g. `"admin"`, `"teacher"`, or `"ai"`.
- `confidence` (`*float64`, optional, `omitempty`): present when added by AI (e.g., `0.94`).
- `addedAt` (string, RFC3339 timestamp).

## Implementation Requirements
1. **Custom CoreDNS Plugin (`plugin/schoolfilter`)**:
   - Implement a CoreDNS plugin named `schoolfilter` with `ServeDNS`, setup/parse logic for the `Corefile` directive (e.g. `schoolfilter ./blocklist.json 127.0.0.1`), and safe concurrent read access (`sync.RWMutex`) to the loaded blocklist.
   - Normalize incoming `qname` (lowercase, trim trailing dot) and check exact match + subdomain match.
   - For blocked `A` (and optionally `AAAA` -> `::` or sinkhole IP) queries, craft a proper DNS response pointing to the configured sinkhole IP with a short TTL (e.g., `60`s). For non-blocked queries, call `plugin.NextOrFailure(...)`.
   - Wire `schoolfilter` cleanly into the CoreDNS build/registration so `schoolfilter` works in `Corefile`.
2. **Custom Block Page (`blockpage.html` + HTTP handler)**:
   - Create a clean, responsive standalone HTML page (`blockpage.html`) styled for a school network block notice.
   - Start a lightweight HTTP server (e.g., `:8080`, configurable/non-intrusive) in the background or alongside `main.go` that looks up the requested `Host` against `blocklist.json` and renders `blockpage.html` with the domain, reason, `addedBy`, and `confidence` (if set).
3. **Sample Files & Tests**:
   - Provide a sample `blocklist.json` with both an `"admin"` entry and an `"ai"` entry (with `confidence`).
   - Provide an updated `Corefile` example (`schoolfilter ./blocklist.json 127.0.0.1` before `log` and `forward . 1.1.1.1`).
   - Add unit tests for domain & subdomain matching and JSON parsing (`go test ./...`).
   - Keep code modular, idiomatic Go, well-commented, and easy to extend later with NATS/Postgres/control-plane updates without putting control-plane I/O on the DNS hot path.

Please inspect the existing repository files first, explain the changes clearly, and provide exact commands to build and test on Linux (`go mod tidy`, `go test ./...`, `go build .`, and `dig` / `curl` verification).