package schoolfilter

import (
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
