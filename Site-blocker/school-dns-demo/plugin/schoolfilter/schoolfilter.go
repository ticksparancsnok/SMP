// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

package schoolfilter

import (
	"context"
	"crypto/subtle"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/coredns/caddy"
	"github.com/coredns/coredns/core/dnsserver"
	"github.com/coredns/coredns/plugin"
	"github.com/coredns/coredns/request"
	"github.com/miekg/dns"
)

// The block page, in Hungarian and English (same wording and layout; keep them in step).
//
//go:embed blockpage.html blockpage.en.html
var blockPage embed.FS

// blockPageFiles: the template for each language (see Blocklist.Language).
var blockPageFiles = map[string]string{"hu": "blockpage.html", "en": "blockpage.en.html"}

// Version is [Site-blocker] of Version.toml in the SMP repository root (the only place
// SMP's versions are written), stamped in at build time by the Dockerfile:
//
//	go build -ldflags "-X 'school-dns-demo/plugin/schoolfilter.Version=1.0.0 A'"
//
// A plain `go build` leaves "dev".
var Version = "dev"

var blockPageTemplate = template.Must(template.ParseFS(blockPage, "blockpage.html", "blockpage.en.html"))

type SchoolFilter struct {
	Next          plugin.Handler
	blocklist     *Blocklist
	sinkhole      net.IP
	httpAddr      string
	blocklistPath string
	syncToken     string
	server        *http.Server
}

func init() {
	plugin.Register("schoolfilter", setup)
}

func (s *SchoolFilter) Name() string { return "schoolfilter" }

func (s *SchoolFilter) ServeDNS(ctx context.Context, w dns.ResponseWriter, r *dns.Msg) (int, error) {
	state := request.Request{W: w, Req: r}
	_, blocked := s.blocklist.Match(state.Name())
	if !blocked || (state.QType() != dns.TypeA && state.QType() != dns.TypeAAAA) {
		return plugin.NextOrFailure(s.Name(), s.Next, ctx, w, r)
	}

	reply := new(dns.Msg)
	reply.SetReply(r)
	reply.Authoritative = true
	reply.RecursionAvailable = true
	header := dns.RR_Header{Name: state.Name(), Class: dns.ClassINET, Ttl: 60}
	switch state.QType() {
	case dns.TypeA:
		header.Rrtype = dns.TypeA
		reply.Answer = []dns.RR{&dns.A{Hdr: header, A: s.sinkhole.To4()}}
	case dns.TypeAAAA:
		header.Rrtype = dns.TypeAAAA
		reply.Answer = []dns.RR{&dns.AAAA{Hdr: header, AAAA: net.IPv6loopback}}
	}
	if err := w.WriteMsg(reply); err != nil {
		return dns.RcodeServerFailure, err
	}
	return dns.RcodeSuccess, nil
}

func setup(c *caddy.Controller) error {
	if !c.Next() {
		return nil
	}
	args := c.RemainingArgs()
	if len(args) < 2 || len(args) > 4 {
		return c.Err("schoolfilter requires blocklist path, sinkhole IP, optional HTTP address, and optional sync token")
	}
	// BLOCKLIST_PATH (set by docker compose) puts the synced lists on a data volume,
	// so they survive container rebuilds; the Corefile's path suits a local demo.
	blocklistPath := args[0]
	if env := strings.TrimSpace(os.Getenv("BLOCKLIST_PATH")); env != "" {
		blocklistPath = env
	}
	blocklist, err := LoadBlocklist(blocklistPath)
	if err != nil {
		return plugin.Error("schoolfilter", err)
	}
	// SINKHOLE_IP (set by docker compose from SMP_SINKHOLE_IP) overrides the Corefile:
	// blocked names must resolve to the server's LAN address, where nginx shows the
	// block page; the Corefile's 127.0.0.1 only suits a local demo.
	sinkholeArg := args[1]
	if env := strings.TrimSpace(os.Getenv("SINKHOLE_IP")); env != "" {
		sinkholeArg = env
	}
	sinkhole := net.ParseIP(sinkholeArg)
	if sinkhole == nil || sinkhole.To4() == nil {
		return c.Errf("invalid IPv4 sinkhole address %q", sinkholeArg)
	}
	httpAddr := ":8080"
	if len(args) == 3 {
		httpAddr = args[2]
	}
	syncToken := os.Getenv("DNS_SYNC_TOKEN")
	if len(args) == 4 {
		syncToken = args[3]
	}
	filter := &SchoolFilter{
		blocklist:     blocklist,
		sinkhole:      sinkhole,
		httpAddr:      httpAddr,
		blocklistPath: blocklistPath,
		syncToken:     syncToken,
	}

	log.Printf("[INFO] schoolfilter: SMP website blocker %s", Version)
	if syncToken == "" {
		log.Printf("[WARNING] schoolfilter: DNS_SYNC_TOKEN is not set; the /api/blocklist/* endpoints are disabled (DNS filtering and the block page still work)")
	}

	c.OnStartup(func() error {
		return filter.startHTTP()
	})
	c.OnShutdown(func() error {
		return filter.stopHTTP()
	})
	dnsserver.GetConfig(c).AddPlugin(func(next plugin.Handler) plugin.Handler {
		filter.Next = next
		return filter
	})
	return nil
}

func (s *SchoolFilter) startHTTP() error {
	listener, err := net.Listen("tcp", s.httpAddr)
	if err != nil {
		return fmt.Errorf("listen for block page on %s: %w", s.httpAddr, err)
	}
	// The block page port is reachable by every client on the school network,
	// so slow clients must not be able to hold connections open forever.
	s.server = &http.Server{Handler: s.routes(), ReadHeaderTimeout: 10 * time.Second}
	go func() {
		if err := s.server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("schoolfilter block page stopped: %v", err)
		}
	}()
	return nil
}

// routes serves the public block page at "/" and the blocklist API under
// /api/blocklist/. The block page port is open to every client on the school
// network, so every API endpoint requires the shared sync token.
func (s *SchoolFilter) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/blocklist/import", s.requireToken(s.handleImport))
	mux.HandleFunc("/api/blocklist/entries", s.requireToken(s.handleEntryCreate))
	mux.HandleFunc("/api/blocklist/sync", s.requireToken(s.handleSync))
	mux.HandleFunc("/api/blocklist/status", s.requireToken(s.handleStatus))
	mux.HandleFunc("/", s.handleBlockPage)
	return mux
}

// requireToken accepts only "Authorization: Bearer <DNS_SYNC_TOKEN>". With no
// token configured the API is disabled rather than open.
func (s *SchoolFilter) requireToken(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.syncToken == "" {
			http.Error(w, "blocklist API disabled: DNS_SYNC_TOKEN is not configured", http.StatusServiceUnavailable)
			return
		}
		token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
		if !ok || subtle.ConstantTimeCompare([]byte(token), []byte(s.syncToken)) != 1 {
			w.Header().Set("WWW-Authenticate", "Bearer")
			http.Error(w, "invalid or missing sync token", http.StatusUnauthorized)
			return
		}
		next(w, r)
	}
}

func (s *SchoolFilter) stopHTTP() error {
	if s.server == nil {
		return nil
	}
	return s.server.Shutdown(context.Background())
}

func frontendUsername(r *http.Request) (string, error) {
	username := strings.TrimSpace(r.Header.Get("X-Username"))
	if username == "" {
		username = strings.TrimSpace(r.FormValue("username"))
	}
	if username == "" {
		return "", errors.New("username is required from the frontend")
	}
	if strings.ContainsAny(username, "\r\n") {
		return "", errors.New("invalid username")
	}
	return username, nil
}

func (s *SchoolFilter) handleImport(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if err := r.ParseMultipartForm(10 << 20); err != nil {
		http.Error(w, "invalid multipart hosts upload", http.StatusBadRequest)
		return
	}
	username, err := frontendUsername(r)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	file, _, err := r.FormFile("hosts")
	if err != nil {
		http.Error(w, "hosts file is required", http.StatusBadRequest)
		return
	}
	defer file.Close()

	domains, err := ParseHosts(file)
	if err != nil {
		http.Error(w, "could not parse hosts file", http.StatusBadRequest)
		return
	}
	now := time.Now().UTC().Format(time.RFC3339)
	for _, domain := range domains {
		if err := s.blocklist.Add(Entry{
			Domain:            domain,
			IncludeSubdomains: true,
			Reason:            "imported",
			AddedBy:           "imported by: " + username,
			AddedAt:           now,
		}); err != nil {
			http.Error(w, "invalid imported domain", http.StatusBadRequest)
			return
		}
	}
	if err := s.blocklist.Save(s.blocklistPath); err != nil {
		http.Error(w, "could not persist blocklist", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_ = json.NewEncoder(w).Encode(map[string]any{"imported": len(domains)})
}

func (s *SchoolFilter) handleEntryCreate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var request struct {
		Username          string   `json:"username"`
		Domain            string   `json:"domain"`
		IncludeSubdomains *bool    `json:"includeSubdomains"`
		Reason            string   `json:"reason"`
		Confidence        *float64 `json:"confidence,omitempty"`
	}
	if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	username := strings.TrimSpace(request.Username)
	if username == "" {
		username = strings.TrimSpace(r.Header.Get("X-Username"))
	}
	if username == "" || strings.ContainsAny(username, "\r\n") {
		http.Error(w, "username is required from the frontend", http.StatusBadRequest)
		return
	}
	includeSubdomains := true
	if request.IncludeSubdomains != nil {
		includeSubdomains = *request.IncludeSubdomains
	}
	entry := Entry{
		Domain:            request.Domain,
		IncludeSubdomains: includeSubdomains,
		Reason:            strings.TrimSpace(request.Reason),
		AddedBy:           username,
		Confidence:        request.Confidence,
		AddedAt:           time.Now().UTC().Format(time.RFC3339),
	}
	if err := s.blocklist.Add(entry); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := s.blocklist.Save(s.blocklistPath); err != nil {
		http.Error(w, "could not persist blocklist", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_ = json.NewEncoder(w).Encode(map[string]string{"domain": entry.Domain})
}

func (s *SchoolFilter) handleSync(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var payload blocklistFile
	if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
		http.Error(w, "invalid blocklist JSON", http.StatusBadRequest)
		return
	}
	// Checked before anything changes: a bad sync leaves the current lists in place.
	if payload.BlockPageLanguage != "" && !validLanguage(payload.BlockPageLanguage) {
		http.Error(w, "blockPageLanguage must be \"hu\" or \"en\"", http.StatusBadRequest)
		return
	}
	if err := s.blocklist.Replace(payload.Entries, payload.Allowlist); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	_ = s.blocklist.SetLanguage(payload.BlockPageLanguage) // checked above; "" = the default
	if err := s.blocklist.Save(s.blocklistPath); err != nil {
		http.Error(w, "could not persist blocklist", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]int{"entries": len(payload.Entries), "allowlist": len(payload.Allowlist)})
}

func (s *SchoolFilter) handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status":    "ok",
		"version":   Version,
		"entries":   s.blocklist.Count(),
		"allowlist": s.blocklist.AllowCount(),
		// the block page's default language (blockPageLanguage of the last sync)
		"blockPageLanguage": s.blocklist.Language(),
	})
}

func (s *SchoolFilter) handleBlockPage(w http.ResponseWriter, r *http.Request) {
	host := r.Host
	if hostName, _, err := net.SplitHostPort(host); err == nil {
		host = hostName
	}
	host = strings.TrimSuffix(host, ".")
	entry, ok := s.blocklist.Match(host)
	if !ok {
		http.NotFound(w, r)
		return
	}
	// The visitor's choice (the page's HU | EN switch: ?lang=), else the school's default.
	lang := r.URL.Query().Get("lang")
	if !validLanguage(lang) {
		lang = s.blocklist.Language()
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Language", lang)
	w.Header().Set("Cache-Control", "no-store") // it depends on the blocklist and ?lang=
	if err := blockPageTemplate.ExecuteTemplate(w, blockPageFiles[lang], struct {
		Domain     string
		Reason     string
		AddedBy    string
		Confidence *float64
	}{host, entry.Reason, entry.AddedBy, entry.Confidence}); err != nil {
		log.Printf("render block page: %v", err)
	}
}
