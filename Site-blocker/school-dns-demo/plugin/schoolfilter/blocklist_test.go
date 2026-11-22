package schoolfilter

import (
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
