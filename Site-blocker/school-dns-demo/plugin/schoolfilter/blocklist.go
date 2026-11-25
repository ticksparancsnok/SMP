// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

package schoolfilter

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

type Entry struct {
	Domain            string   `json:"domain"`
	IncludeSubdomains bool     `json:"includeSubdomains"`
	Reason            string   `json:"reason,omitempty"`
	AddedBy           string   `json:"addedBy"`
	Confidence        *float64 `json:"confidence,omitempty"`
	AddedAt           string   `json:"addedAt"`
}

// blocklistFile is both the persisted blocklist.json and the sync payload.
type blocklistFile struct {
	Entries   []Entry `json:"entries"`
	Allowlist []Entry `json:"allowlist,omitempty"`
	// The block page's language unless the visitor picks one ("hu" or "en"; the school
	// chooses it on SMP's Site blocker page). Empty: DefaultLanguage.
	BlockPageLanguage string `json:"blockPageLanguage,omitempty"`
}

// The block page languages (blockpage.html, blockpage.en.html).
const DefaultLanguage = "hu"

func validLanguage(lang string) bool { return lang == "hu" || lang == "en" }

// Blocklist holds the blocked domains and the allowlist, which is checked first
// (see Match). Public suffixes (com, co.uk, ...) are kept off both lists by the
// control plane (server/smp/modules/dns/domains.py).
type Blocklist struct {
	mu       sync.RWMutex
	entries  map[string]Entry
	allow    map[string]Entry
	language string // the block page's default language
}

var defaultPrivateDNSDomains = func() map[string]Entry {
	domains := []string{
		"cloudflare-dns.com",
		"dns.adguard-dns.com",
		"dns.adguard.com",
		"dns.google",
		"dns.nextdns.io",
		"dns.quad9.net",
		"doh.cleanbrowsing.org",
		"doh.opendns.com",
		"mask-h2.icloud.com",
		"mask.icloud.com",
		"mozilla.cloudflare-dns.com",
		"one.one.one.one",
	}
	entries := make(map[string]Entry, len(domains))
	for _, domain := range domains {
		entries[domain] = Entry{Domain: domain, IncludeSubdomains: true, Reason: "Private DNS endpoint blocked by default", AddedBy: "built-in"}
	}
	return entries
}()

// SetLanguage sets the block page's default language; "" means DefaultLanguage.
func (b *Blocklist) SetLanguage(lang string) error {
	if lang == "" {
		lang = DefaultLanguage
	}
	if !validLanguage(lang) {
		return fmt.Errorf("blockPageLanguage must be \"hu\" or \"en\", not %q", lang)
	}
	b.mu.Lock()
	b.language = lang
	b.mu.Unlock()
	return nil
}

// Language is the block page's default language.
func (b *Blocklist) Language() string {
	b.mu.RLock()
	defer b.mu.RUnlock()
	if b.language == "" {
		return DefaultLanguage
	}
	return b.language
}

func (b *Blocklist) Add(entry Entry) error {
	normalized, err := normalizeDomain(entry.Domain)
	if err != nil {
		return err
	}
	entry.Domain = normalized
	b.mu.Lock()
	b.entries[normalized] = entry
	b.mu.Unlock()
	return nil
}

func toMap(entries []Entry, list string) (map[string]Entry, error) {
	out := make(map[string]Entry, len(entries))
	for i, entry := range entries {
		normalized, err := normalizeDomain(entry.Domain)
		if err != nil {
			return nil, fmt.Errorf("%s entry %d: %w", list, i, err)
		}
		entry.Domain = normalized
		out[normalized] = entry
	}
	return out, nil
}

// Replace swaps in both lists at once (a sync from the control plane).
func (b *Blocklist) Replace(entries, allowlist []Entry) error {
	blocked, err := toMap(entries, "blocklist")
	if err != nil {
		return err
	}
	allowed, err := toMap(allowlist, "allowlist")
	if err != nil {
		return err
	}
	b.mu.Lock()
	b.entries, b.allow = blocked, allowed
	b.mu.Unlock()
	return nil
}

func sortedEntries(m map[string]Entry) []Entry {
	out := make([]Entry, 0, len(m))
	for _, entry := range m {
		out = append(out, entry)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Domain < out[j].Domain })
	return out
}

func (b *Blocklist) Snapshot() blocklistFile {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return blocklistFile{Entries: sortedEntries(b.entries), Allowlist: sortedEntries(b.allow), BlockPageLanguage: b.language}
}

func (b *Blocklist) Count() int {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return len(b.entries)
}

func (b *Blocklist) AllowCount() int {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return len(b.allow)
}

func (b *Blocklist) Save(path string) error {
	file := b.Snapshot()
	data, err := json.MarshalIndent(file, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal blocklist: %w", err)
	}
	data = append(data, '\n')
	if err := os.WriteFile(path, data, 0644); err != nil {
		return fmt.Errorf("write blocklist: %w", err)
	}
	return nil
}

// LoadBlocklist reads the persisted lists. A missing file means "nothing synced
// yet" (a fresh data volume): start with empty lists, the first sync from the
// control plane fills and saves them.
func LoadBlocklist(path string) (*Blocklist, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return &Blocklist{entries: map[string]Entry{}, allow: map[string]Entry{}}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read blocklist: %w", err)
	}

	var file blocklistFile
	if err := json.Unmarshal(data, &file); err != nil {
		return nil, fmt.Errorf("parse blocklist: %w", err)
	}

	for list, entries := range map[string][]Entry{"blocklist": file.Entries, "allowlist": file.Allowlist} {
		for i, entry := range entries {
			if entry.AddedBy == "" {
				return nil, fmt.Errorf("%s entry %d: addedBy is required", list, i)
			}
			if _, err := time.Parse(time.RFC3339, entry.AddedAt); err != nil {
				return nil, fmt.Errorf("%s entry %d: addedAt must be RFC3339: %w", list, i, err)
			}
		}
	}
	blocklist := &Blocklist{}
	if err := blocklist.Replace(file.Entries, file.Allowlist); err != nil {
		return nil, err
	}
	if err := blocklist.SetLanguage(file.BlockPageLanguage); err != nil {
		return nil, err
	}
	return blocklist, nil
}

// Match reports the blocklist entry that blocks domain. Decision order:
//
//  1. allowlist  -> allowed (exact, or a parent with includeSubdomains)
//  2. blocklist  -> blocked (same matching)
//  3. otherwise  -> allowed
//
// TODO(evaluate, issue #3): total allowlist ("whitelist") control, i.e. step 3 becomes
// "blocked" for some clients (exam mode, primary-school labs). Needs per-client
// policies first (readme.md, section 4: ClientMap -> Policy.Mode).
func (b *Blocklist) Match(domain string) (Entry, bool) {
	normalized, err := normalizeDomain(domain)
	if err != nil {
		return Entry{}, false
	}

	b.mu.RLock()
	defer b.mu.RUnlock()

	if _, allowed := lookup(b.allow, normalized); allowed {
		return Entry{}, false
	}
	if entry, blocked := lookup(defaultPrivateDNSDomains, normalized); blocked {
		return entry, true
	}
	return lookup(b.entries, normalized)
}

// lookup finds the entry for domain itself, or for its nearest parent that has
// includeSubdomains set.
func lookup(entries map[string]Entry, domain string) (Entry, bool) {
	if entry, ok := entries[domain]; ok {
		return entry, true
	}
	for start := strings.IndexByte(domain, '.'); start >= 0; {
		parent := domain[start+1:]
		if entry, ok := entries[parent]; ok && entry.IncludeSubdomains {
			return entry, true
		}
		next := strings.IndexByte(domain[start+1:], '.')
		if next < 0 {
			break
		}
		start += next + 1
	}
	return Entry{}, false
}

func normalizeDomain(domain string) (string, error) {
	normalized := strings.ToLower(strings.TrimSuffix(strings.TrimSpace(domain), "."))
	if normalized == "" {
		return "", fmt.Errorf("domain is required")
	}
	if strings.ContainsAny(normalized, " /\\") {
		return "", fmt.Errorf("invalid domain %q", domain)
	}
	return normalized, nil
}
