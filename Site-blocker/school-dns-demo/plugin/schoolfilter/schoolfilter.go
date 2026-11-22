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

//go:embed blockpage.html
var blockPage embed.FS

var blockPageTemplate = template.Must(template.ParseFS(blockPage, "blockpage.html"))

type SchoolFilter struct {
	Next     plugin.Handler
	blocklist *Blocklist
	sinkhole net.IP
	httpAddr string
	blocklistPath string
	syncToken string
	server   *http.Server
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
	blocklist, err := LoadBlocklist(args[0])
	if err != nil {
		return plugin.Error("schoolfilter", err)
	}
	sinkhole := net.ParseIP(args[1])
	if sinkhole == nil || sinkhole.To4() == nil {
		return c.Errf("invalid IPv4 sinkhole address %q", args[1])
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
		blocklist: blocklist,
		sinkhole:  sinkhole,
		httpAddr:  httpAddr,
		blocklistPath: args[0],
		syncToken: syncToken,
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
	mux := http.NewServeMux()
	mux.HandleFunc("/api/blocklist/import", s.handleImport)
	mux.HandleFunc("/api/blocklist/entries", s.handleEntryCreate)
	mux.HandleFunc("/api/blocklist/sync", s.handleSync)
	mux.HandleFunc("/api/blocklist/status", s.handleStatus)
	mux.HandleFunc("/", s.handleBlockPage)
	listener, err := net.Listen("tcp", s.httpAddr)
	if err != nil {
		return fmt.Errorf("listen for block page on %s: %w", s.httpAddr, err)
	}
	s.server = &http.Server{Handler: mux}
	go func() {
		if err := s.server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("schoolfilter block page stopped: %v", err)
		}
	}()
	return nil
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
		Username         string   `json:"username"`
		Domain           string   `json:"domain"`
		IncludeSubdomains *bool    `json:"includeSubdomains"`
		Reason           string   `json:"reason"`
		Confidence       *float64 `json:"confidence,omitempty"`
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
	if err := s.blocklist.Add(entry); err != {
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
	if s.syncToken != "" {
		token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if len(token) != len(s.syncToken) || subtle.ConstantTimeCompare([]byte(token), []byte(s.syncToken)) != 1 {
			http.Error(w, "invalid sync token", http.StatusUnauthorized)
			return
		}
	}
	var payload blocklistFile
	if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
		http.Error(w, "invalid blocklist JSON", http.StatusBadRequest)
		return
	}
	if err := s.blocklist.Replace(payload.Entries); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := s.blocklist.Save(s.blocklistPath); err != nil {
		http.Error(w, "could not persist blocklist", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]int{"entries": len(payload.Entries)})
}

func (s *SchoolFilter) handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status": "ok",
		"entries": s.blocklist.Count(),
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
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := blockPageTemplate.Execute(w, struct {
		Domain     string
		Reason     string
		AddedBy    string
		Confidence *float64
	}{host, entry.Reason, entry.AddedBy, entry.Confidence}); err != nil {
		log.Printf("render block page: %v", err)
	}
}
