// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

package main

import (
	_ "school-dns-demo/plugin/schoolfilter"

	"github.com/coredns/coredns/core/dnsserver"
	_ "github.com/coredns/coredns/core/plugin"
	"github.com/coredns/coredns/coremain"
)

func main() {
	directives := make([]string, 0, len(dnsserver.Directives)+1)
	for _, directive := range dnsserver.Directives {
		if directive == "cache" {
			directives = append(directives, "schoolfilter")
		}
		directives = append(directives, directive)
	}
	if err := dnsserver.SetDirectives(directives); err != nil {
		panic(err)
	}
	coremain.Run()
}
