// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

package schoolfilter

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestMatchExactAndSubdomain(t *testing.T) {
	path := filepath.Join(t.TempDir(), "blocklist.json")
	content := `{"entries":[
		{"domain":"Example.COM.","includeSubdomains":true,"addedBy":"admin","addedAt":"2026-11-21T20:00:00Z"},
		{"domain":"exact.test","includeSubdomains":false,"addedBy":"teacher","addedAt":"2026-11-21T20:00:00Z"}
	]}`
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	list, err := LoadBlocklist(path)
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		domain string
		want   bool
	}{
		{"example.com.", true},
		{"www.example.com", true},
		{"notexample.com", false},
		{"sub.exact.test", false},
		{"exact.test", true},
	} {
		if _, ok := list.Match(test.domain); ok != test.want {
			t.Errorf("Match(%q) = %v, want %v", test.domain, ok, test.want)
		}
	}
}

func TestLoadBlocklistPreservesConfidence(t *testing.T) {
	path := filepath.Join(t.TempDir(), "blocklist.json")
	if err := os.WriteFile(path, []byte(`{"entries":[{"domain":"ai.test","includeSubdomains":true,"addedBy":"ai","confidence":0.94,"addedAt":"2026-11-21T20:00:00Z"}]}`), 0600); err != nil {
		t.Fatal(err)
	}
	list, err := LoadBlocklist(path)
	if err != nil {
		t.Fatal(err)
	}
	entry, ok := list.Match("ai.test")
	if !ok || entry.Confidence == nil || *entry.Confidence != 0.94 {
		t.Fatalf("unexpected entry: %#v", entry)
	}
}

func TestParseHosts(t *testing.T) {
	hosts := `# comment
0.0.0.0 Example.COM. example.net
127.0.0.1 localhost
::1 localhost
255.255.255.255 broadcasthost
fe80::1 ignored.example
10.0.0.1 one.test two.test
10.0.0.2 one.test
`
	domains, err := ParseHosts(strings.NewReader(hosts))
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"example.com", "example.net", "one.test", "two.test"}
	if !reflect.DeepEqual(domains, want) {
		t.Fatalf("ParseHosts() = %#v, want %#v", domains, want)
	}
}

func TestParseCommonBlocklistFormats(t *testing.T) {
	content := "\uFEFFplain.example\n" + `# hosts format
0.0.0.0 hosts.example
! domain-list comment
||adblock.example^$third-party
@@||allowed.example^
||withdrawn.example^$badfilter
google.com##.ad-banner
youtube.com###masthead-ad
reddit.com,example.org#@#.promoted
example.net#?#div:has(> .ad)
example.info#$#body { overflow: auto !important; }
example.biz#%#//scriptlet('abort-on-property-read', 'ads')
0.0.0.0 commented.example ## a trailing comment
glued.example#not-a-comment
`
	domains, err := ParseHosts(strings.NewReader(content))
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"plain.example", "hosts.example", "adblock.example", "commented.example"}
	if !reflect.DeepEqual(domains, want) {
		t.Fatalf("ParseHosts() = %#v, want %#v", domains, want)
	}
}

func TestPrivateDNSDefaultsAreBlockedAndAllowlistCanOverride(t *testing.T) {
	list := &Blocklist{}
	if err := list.Replace(nil, nil); err != nil {
		t.Fatal(err)
	}
	for _, domain := range []string{"dns.google", "mozilla.cloudflare-dns.com", "mask.icloud.com", "sub.mask-h2.icloud.com"} {
		if _, blocked := list.Match(domain); !blocked {
			t.Errorf("%s should be blocked by default", domain)
		}
	}
	if err := list.Replace(nil, []Entry{{Domain: "mask.icloud.com", IncludeSubdomains: true}}); err != nil {
		t.Fatal(err)
	}
	if _, blocked := list.Match("mask.icloud.com"); blocked {
		t.Error("an explicit allowlist entry should override the default")
	}
}

func BenchmarkBlocklistMatch(b *testing.B) {
	for _, size := range []int{1_000, 100_000} {
		b.Run(fmt.Sprintf("%d_entries", size), func(b *testing.B) {
			entries := make([]Entry, size)
			for i := range entries {
				entries[i] = Entry{Domain: fmt.Sprintf("site%d.example", i)}
			}
			list := &Blocklist{}
			if err := list.Replace(entries, nil); err != nil {
				b.Fatal(err)
			}
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				list.Match("not-listed.example")
			}
		})
	}
}

func TestAllowlistIsCheckedBeforeBlocklist(t *testing.T) {
	list := &Blocklist{}
	if err := list.Replace(
		[]Entry{{Domain: "example.com", IncludeSubdomains: true}, {Domain: "exact.test"}},
		[]Entry{{Domain: "edu.example.com", IncludeSubdomains: true}, {Domain: "exact.test"}},
	); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		domain string
		want   bool
	}{
		{"www.example.com", true},     // blocklist (parent with includeSubdomains)
		{"edu.example.com", false},    // allowlist wins over the blocklist
		{"x.edu.example.com.", false}, // ...for its subdomains too
		{"exact.test", false},         // on both lists: allowed
		{"other.org", false},          // on neither: allowed
	} {
		if _, ok := list.Match(test.domain); ok != test.want {
			t.Errorf("Match(%q) = %v, want %v", test.domain, ok, test.want)
		}
	}
}

func TestSyncedAllowlistIsPersisted(t *testing.T) {
	s, h := newTestFilter(t, testToken)
	body := `{"entries":[{"domain":"example.com","includeSubdomains":true,"addedBy":"admin","addedAt":"2026-11-22T00:00:00Z"}],
	          "allowlist":[{"domain":"ok.example.com","includeSubdomains":false,"addedBy":"teacher","addedAt":"2026-11-22T00:00:00Z"}]}`
	req := httptest.NewRequest(http.MethodPost, "/api/blocklist/sync", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+testToken)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("sync: %d %s", rec.Code, rec.Body.String())
	}
	reloaded, err := LoadBlocklist(s.blocklistPath) // what a restart would read
	if err != nil {
		t.Fatal(err)
	}
	if _, blocked := reloaded.Match("ok.example.com"); blocked {
		t.Error("allowlisted domain blocked after reload")
	}
	if _, blocked := reloaded.Match("www.example.com"); !blocked {
		t.Error("blocked domain allowed after reload")
	}
	if reloaded.AllowCount() != 1 || reloaded.Count() != 1 {
		t.Errorf("reloaded counts: %d blocked, %d allowed", reloaded.Count(), reloaded.AllowCount())
	}
}

func TestMissingBlocklistFileStartsEmpty(t *testing.T) {
	path := filepath.Join(t.TempDir(), "data", "blocklist.json") // fresh volume: nothing synced yet
	list, err := LoadBlocklist(path)
	if err != nil {
		t.Fatalf("missing file should mean empty lists, got %v", err)
	}
	if list.Count() != 0 || list.AllowCount() != 0 {
		t.Fatalf("want empty lists, got %d blocked / %d allowed", list.Count(), list.AllowCount())
	}
	if err := list.Replace([]Entry{{Domain: "example.com", AddedBy: "admin", AddedAt: "2026-11-22T00:00:00Z"}}, nil); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := list.Save(path); err != nil {
		t.Fatal(err)
	}
	if reloaded, err := LoadBlocklist(path); err != nil || reloaded.Count() != 1 {
		t.Fatalf("after the first sync the file must load: %v", err)
	}
}
