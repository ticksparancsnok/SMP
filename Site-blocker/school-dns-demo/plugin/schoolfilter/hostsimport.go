// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

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
		line := strings.TrimSpace(strings.TrimPrefix(scanner.Text(), "\uFEFF"))
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "!") {
			continue
		}
		if comment := strings.IndexByte(line, '#'); comment >= 0 {
			line = strings.TrimSpace(line[:comment])
		}
		if line == "" {
			continue
		}

		tokens := strings.Fields(line)
		firstDomain := 0
		lastDomain := len(tokens)
		if ip := net.ParseIP(strings.TrimSpace(tokens[0])); ip != nil {
			if shouldSkipHostsAddress(strings.ToLower(ip.String())) {
				continue
			}
			firstDomain = 1
		} else {
			lastDomain = 1
			if strings.HasPrefix(tokens[0], "@@") {
				continue
			}
		}
		for _, token := range tokens[firstDomain:lastDomain] {
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
	domain = strings.TrimPrefix(domain, "||")
	domain = strings.TrimPrefix(domain, "|")
	if modifier := strings.IndexByte(domain, '$'); modifier >= 0 {
		domain = domain[:modifier]
	}
	if ruleEnd := strings.IndexByte(domain, '^'); ruleEnd >= 0 {
		domain = domain[:ruleEnd]
	}
	domain = strings.TrimSuffix(domain, ".")
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
