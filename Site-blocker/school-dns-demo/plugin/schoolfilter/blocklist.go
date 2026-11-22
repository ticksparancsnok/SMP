package schoolfilter

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

type Entry struct {
	Domain           string   `json:"domain"`
	IncludeSubdomains bool     `json:"includeSubdomains"`
	Reason           string   `json:"reason,omitempty"`
	AddedBy          string   `json:"addedBy"`
	Confidence       *float64 `json:"confidence,omitempty"`
	AddedAt          string   `json:"addedAt"`
}

type blocklistFile struct {
	Entries []Entry `json:"entries"`
}

type Blocklist struct {
	mu      sync.RWMutex
	entries map[string]Entry
}

func (b *Blocklist) Add(entry Entry) error {
	normalized, err := normalizeDomain(entry.Domain)
	if err != {
		return err
	}
	entry.Domain = normalized
	b.mu.Lock()
	b.entries[normalized] = entry
	b.mu.Unlock()
	return nil
}

func (b *Blocklist) Replace(entries []Entry) error {
	replaced := make(map[string]Entry, len(entries))
	for i, entry := range entries {
		normalized, err := normalizeDomain(entry.Domain)
		if err != nil {
			return fmt.Errorf("entry %d: %w", i, err)
		}
		entry.Domain = normalized
		replaced[normalized] = entry
	}
	b.mu.Lock()
	b.entries = replaced
	b.mu.Unlock()
	return nil
}

func (b *Blocklist) Snapshot() blocklistFile {
	b.mu.RLock()
	defer b.mu.RUnlock()
	file := blocklistFile{Entries: make([]Entry, 0, len(b.entries))}
	for _, entry := range b.entries {
		file.Entries = append(file.Entries, entry)
	}

	func (b *Blocklist) Count() int {
		b.mu.RLock()
		defer b.mu.RUnlock()
		return len(b.entries)
	}
	sort.Slice(file.Entries, func(i, j int) bool {
		return file.Entries[i].Domain < file.Entries[j].Domain
	})
	return file
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

func LoadBlocklist(path string) (*Blocklist, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read blocklist: %w", err)
	}

	var file blocklistFile
	if err := json.Unmarshal(data, &file); err != nil {
		return nil, fmt.Errorf("parse blocklist: %w", err)
	}

	blocklist := &Blocklist{entries: make(map[string]Entry, len(file.Entries))}
	for i, entry := range file.Entries {
		normalized, err := normalizeDomain(entry.Domain)
		if err != nil {
			return nil, fmt.Errorf("entry %d: %w", i, err)
		}
		if entry.AddedBy == "" {
			return nil, fmt.Errorf("entry %d: addedBy is required", i)
		}
		if _, err := time.Parse(time.RFC3339, entry.AddedAt); err != nil {
			return nil, fmt.Errorf("entry %d: addedAt must be RFC3339: %w", i, err)
		}
		entry.Domain = normalized
		blocklist.entries[normalized] = entry
	}
	return blocklist, nil
}

func (b *Blocklist) Match(domain string) (Entry, bool) {
	normalized, err := normalizeDomain(domain)
	if err != nil {
		return Entry{}, false
	}

	b.mu.RLock()
	defer b.mu.RUnlock()

	if entry, ok := b.entries[normalized]; ok {
		return entry, true
	}
	for start := strings.IndexByte(normalized, '.'); start >= 0; {
		parent := normalized[start+1:]
		if entry, ok := b.entries[parent]; ok && entry.IncludeSubdomains {
			return entry, true
		}
		next := strings.IndexByte(normalized[start+1:], '.')
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
