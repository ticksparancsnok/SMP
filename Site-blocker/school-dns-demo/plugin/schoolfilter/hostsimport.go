package schoolfilter

import (
	"bufio"
	"io"
	"net"
	"strings"
)

func ParseHosts(reader io.Reader) ([]string, error) {
	domains := make([]string, 0)
	seen := make(map[string]struct{})
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 1024), 1024*1024)

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if comment := strings.IndexByte(line, '#'); comment >= 0 {
			line = strings.TrimSpace(line[:comment])
		}
		if line == "" {
			continue
		}

		tokens := strings.Fields(line)
		if len(tokens) < 2 {
			continue
		}
		ip := net.ParseIP(strings.TrimSpace(tokens[0]))
		if ip == nil {
			continue
		}
		ipText := strings.ToLower(ip.String())
		if shouldSkipHostsAddress(ipText) {
			continue
		}
		for _, token := range tokens[1:] {
			domain, ok := normalizeImportedDomain(token)
			if !ok {
				continue
			}
			if _, exists := seen[domain]; exists {
				continue
			}
			seen[domain] = struct{}{}
			domains = append(domains, domain)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return domains, nil
}

func shouldSkipHostsAddress(address string) bool {
	if address == "0.0.0.0" || address == "127.0.0.1" {
		return false
	}
	ip := net.ParseIP(address)
	if ip == nil {
		return true
	}
	if ip.IsLoopback() || ip.IsMulticast() || ip.IsUnspecified() {
		return true
	}
	ip = ip.To4()
	if ip != nil {
		return ip[0] == 224 || ip[0] == 255
	}
	ip16 := ip.To16()
	if ip16 == nil {
		return true
	}
	return ip16[0] == 0xfe || ip16[0] == 0xff
}

func normalizeImportedDomain(domain string) (string, bool) {
	domain = strings.ToLower(strings.TrimSpace(strings.TrimSuffix(domain, ".")))
	if domain == "" {
		return "", false
	}
	switch domain {
	case "localhost", "localhost.localdomain", "broadcasthost":
		return "", false
	}
	if strings.ContainsAny(domain, " /\\") {
		return "", false
	}
	return domain, true
}
