// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

package schoolfilter

import (
	"bufio"
	"io"
	"net"
	"regexp"
	"strings"
)

// A '#' starts a comment only at the start of a line or after whitespace:
// "0.0.0.0 ads.example # tracker" is a comment, "example.com##.banner" is not.
var importComment = regexp.MustCompile(`(^|\s)#.*$`)

// Browser ad-blocker rules that hide parts of a page (element hiding, scriptlets:
// "example.com##.banner", "example.com#@#.ad", "#?#", "#$#", "#%#", ...). They name
// the site the rule works on, not a site to block, so the whole line is skipped.
// TODO(evaluate): such per-site rules could become something other than a DNS block.
var cosmeticRule = regexp.MustCompile(`#@?[$?%]{0,2}#`)

// ParseHosts reads hosts files, plain domain lists and Adblock-style "||domain^" lists.
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
		line = strings.TrimSpace(importComment.ReplaceAllString(line, ""))
		if line == "" || cosmeticRule.MatchString(line) {
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
			if isBadfilter(token) {
				continue
			}
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

// isBadfilter: "||example.com^$badfilter" withdraws the list's own rule for
// example.com; it never means "block".
func isBadfilter(rule string) bool {
	_, modifiers, found := strings.Cut(rule, "$")
	if !found {
		return false
	}
	for _, modifier := range strings.Split(modifiers, ",") {
		if strings.TrimSpace(strings.ToLower(modifier)) == "badfilter" {
			return true
		}
	}
	return false
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
	if strings.ContainsAny(domain, " /\\#") {
		return "", false
	}
	return domain, true
}
