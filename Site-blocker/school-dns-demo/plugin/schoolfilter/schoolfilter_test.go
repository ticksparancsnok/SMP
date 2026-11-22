// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

package schoolfilter

import (
	"bytes"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
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
