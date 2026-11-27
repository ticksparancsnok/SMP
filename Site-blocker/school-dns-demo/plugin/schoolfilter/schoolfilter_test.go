// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

package schoolfilter

import (
	"bytes"
	"context"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/miekg/dns"
)

const testToken = "test-sync-token-0123456789"

func newTestFilter(t *testing.T, token string) (*SchoolFilter, http.Handler) {
	t.Helper()
	s := &SchoolFilter{
		blocklist:     &Blocklist{entries: map[string]Entry{}},
		blocklistPath: filepath.Join(t.TempDir(), "blocklist.json"),
		syncToken:     token,
	}
	return s, s.routes()
}

type apiCall struct {
	name        string
	method      string
	path        string
	contentType string
	body        func() []byte
}

func apiCalls(t *testing.T) []apiCall {
	return []apiCall{
		{"sync", http.MethodPost, "/api/blocklist/sync", "application/json", func() []byte {
			return []byte(`{"entries":[{"domain":"synced.example","includeSubdomains":true,"addedBy":"admin","addedAt":"2026-11-22T00:00:00Z"}]}`)
		}},
		{"entries", http.MethodPost, "/api/blocklist/entries", "application/json", func() []byte {
			return []byte(`{"username":"kovacs.janos","domain":"manual.example"}`)
		}},
		{"import", http.MethodPost, "/api/blocklist/import", "", nil},
		{"status", http.MethodGet, "/api/blocklist/status", "", nil},
	}
}

func (c apiCall) request(t *testing.T, auth string) *http.Request {
	t.Helper()
	var body []byte
	contentType := c.contentType
	if c.name == "import" {
		var buf bytes.Buffer
		mw := multipart.NewWriter(&buf)
		_ = mw.WriteField("username", "kovacs.janos")
		fw, err := mw.CreateFormFile("hosts", "hosts.txt")
		if err != nil {
			t.Fatal(err)
		}
		_, _ = fw.Write([]byte("0.0.0.0 imported.example\n"))
		_ = mw.Close()
		body, contentType = buf.Bytes(), mw.FormDataContentType()
	} else if c.body != nil {
		body = c.body()
	}
	req := httptest.NewRequest(c.method, c.path, bytes.NewReader(body))
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if auth != "" {
		req.Header.Set("Authorization", auth)
	}
	return req
}

func TestAPIRejectsMissingOrWrongToken(t *testing.T) {
	for _, call := range apiCalls(t) {
		for _, auth := range []string{"", "Bearer wrong", "Bearer " + testToken + "x", testToken, "Basic " + testToken} {
			s, h := newTestFilter(t, testToken)
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, call.request(t, auth))
			if rec.Code != http.StatusUnauthorized {
				t.Errorf("%s with Authorization %q: status %d, want 401", call.name, auth, rec.Code)
			}
			if n := s.blocklist.Count(); n != 0 {
				t.Errorf("%s with Authorization %q changed the blocklist (%d entries)", call.name, auth, n)
			}
		}
	}
}

func TestAPIDisabledWithoutConfiguredToken(t *testing.T) {
	for _, call := range apiCalls(t) {
		for _, auth := range []string{"", "Bearer ", "Bearer anything"} {
			s, h := newTestFilter(t, "")
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, call.request(t, auth))
			if rec.Code != http.StatusServiceUnavailable {
				t.Errorf("%s with no token configured, Authorization %q: status %d, want 503", call.name, auth, rec.Code)
			}
			if n := s.blocklist.Count(); n != 0 {
				t.Errorf("%s with no token configured changed the blocklist (%d entries)", call.name, n)
			}
		}
	}
}

func TestAPIAcceptsValidToken(t *testing.T) {
	want := map[string]int{"sync": http.StatusOK, "entries": http.StatusCreated, "import": http.StatusCreated, "status": http.StatusOK}
	for _, call := range apiCalls(t) {
		s, h := newTestFilter(t, testToken)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, call.request(t, "Bearer "+testToken))
		if rec.Code != want[call.name] {
			t.Errorf("%s with valid token: status %d, want %d (%s)", call.name, rec.Code, want[call.name], strings.TrimSpace(rec.Body.String()))
		}
		if call.method == http.MethodPost && s.blocklist.Count() != 1 {
			t.Errorf("%s with valid token: blocklist has %d entries, want 1", call.name, s.blocklist.Count())
		}
	}
}

func TestBlockPageStaysPublic(t *testing.T) {
	s, h := newTestFilter(t, testToken)
	if err := s.blocklist.Add(Entry{Domain: "blocked.example", IncludeSubdomains: true, Reason: "test reason", AddedBy: "admin"}); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/", "/some/page", "/api/v1/anything"} {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		req.Host = "www.blocked.example"
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "test reason") {
			t.Errorf("block page %s: status %d, want 200 with the reason", path, rec.Code)
		}
	}
}

type testDNSResponseWriter struct {
	msg *dns.Msg
}

func (w *testDNSResponseWriter) LocalAddr() net.Addr            { return &net.UDPAddr{} }
func (w *testDNSResponseWriter) RemoteAddr() net.Addr           { return &net.UDPAddr{} }
func (w *testDNSResponseWriter) WriteMsg(msg *dns.Msg) error    { w.msg = msg; return nil }
func (w *testDNSResponseWriter) Write(data []byte) (int, error) { return len(data), nil }
func (w *testDNSResponseWriter) Close() error                   { return nil }
func (w *testDNSResponseWriter) TsigStatus() error              { return nil }
func (w *testDNSResponseWriter) TsigTimersOnly(bool)            {}
func (w *testDNSResponseWriter) Hijack()                        {}

func TestBlockedHTTPSAndSVCBQueriesReturnEmptyAnswers(t *testing.T) {
	s := &SchoolFilter{
		blocklist: &Blocklist{entries: map[string]Entry{}, allow: map[string]Entry{}},
	}
	if err := s.blocklist.Add(Entry{Domain: "blocked.example", IncludeSubdomains: true}); err != nil {
		t.Fatal(err)
	}
	for _, qtype := range []uint16{dns.TypeHTTPS, dns.TypeSVCB} {
		request := new(dns.Msg)
		request.SetQuestion("blocked.example.", qtype)
		writer := &testDNSResponseWriter{}
		code, err := s.ServeDNS(context.Background(), writer, request)
		if err != nil || code != dns.RcodeSuccess {
			t.Fatalf("ServeDNS(%d) = (%d, %v), want success", qtype, code, err)
		}
		if writer.msg == nil || writer.msg.Rcode != dns.RcodeSuccess || len(writer.msg.Answer) != 0 {
			t.Errorf("ServeDNS(%d) reply = %#v, want an empty NOERROR answer", qtype, writer.msg)
		}
	}
}

// blockPageFor renders the block page of www.blocked.example at path (e.g. "/?lang=en").
func blockPageFor(t *testing.T, h http.Handler, path string) string {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	req.Host = "www.blocked.example"
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("block page %s: status %d", path, rec.Code)
	}
	return rec.Body.String()
}

func syncLists(t *testing.T, h http.Handler, extra string) *httptest.ResponseRecorder {
	t.Helper()
	body := `{"entries":[{"domain":"blocked.example","includeSubdomains":true,"addedBy":"admin","addedAt":"2026-11-22T00:00:00Z"}]` + extra + `}`
	req := httptest.NewRequest(http.MethodPost, "/api/blocklist/sync", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+testToken)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestBlockPageLanguage(t *testing.T) {
	s, h := newTestFilter(t, testToken)
	if rec := syncLists(t, h, ""); rec.Code != http.StatusOK {
		t.Fatalf("sync: %d %s", rec.Code, rec.Body.String())
	}
	const hu, en = "Ez a webhely blokkolva van", "This website is blocked"

	// Nothing chosen: Hungarian. The visitor's switch (?lang=) wins; nonsense is ignored.
	for path, want := range map[string]string{"/": hu, "/?lang=en": en, "/page?lang=hu": hu, "/?lang=de": hu} {
		if page := blockPageFor(t, h, path); !strings.Contains(page, want) {
			t.Errorf("%s: want %q", path, want)
		}
	}
	if page := blockPageFor(t, h, "/?lang=en"); !strings.Contains(page, "The school's content filter policy") ||
		!strings.Contains(page, `href="?lang=hu"`) {
		t.Error("English page: default reason or the switch back to Hungarian missing")
	}

	// The school's default comes with the sync, and survives a restart.
	if rec := syncLists(t, h, `,"blockPageLanguage":"en"`); rec.Code != http.StatusOK {
		t.Fatalf("sync: %d %s", rec.Code, rec.Body.String())
	}
	if page := blockPageFor(t, h, "/"); !strings.Contains(page, en) {
		t.Error("school default English: page not in English")
	}
	if page := blockPageFor(t, h, "/?lang=hu"); !strings.Contains(page, hu) {
		t.Error("the visitor's choice must win over the school default")
	}
	reloaded, err := LoadBlocklist(s.blocklistPath)
	if err != nil || reloaded.Language() != "en" {
		t.Errorf("after a restart: language %q, err %v", reloaded.Language(), err)
	}

	// A bad value is refused and changes nothing.
	if rec := syncLists(t, h, `,"blockPageLanguage":"de"`); rec.Code != http.StatusBadRequest {
		t.Errorf("bad language: status %d, want 400", rec.Code)
	}
	if s.blocklist.Language() != "en" {
		t.Error("a refused sync changed the language")
	}
}
