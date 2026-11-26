# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)  # ruff: ignore[missing-copyright-notice]
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Domain rules for the DNS module: validation, public suffixes, matching, hosts files.

Decision order for a query (the Go schoolfilter implements the same in Match):

    1. allowlist           -> allowed   (user entries; exact, or a parent with include_subdomains)
    2. built-in blocklist  -> blocked   (BUILTIN_BLOCKLIST: ways around SMP's DNS)
    3. blocklist           -> blocked
    4. anything else       -> allowed

Public suffixes (com, co.uk, hu, ...) are a built-in part of the allowlist: they can
never be put on the blocklist or the allowlist, because one such entry would block
(or unblock) a whole top-level domain for the school. They come from the Public
Suffix List bundled with `tldextract` (its offline snapshot, ICANN section only; the
server never downloads it). Updating the `tldextract` package updates the list.

TODO(evaluate, issue #3): total allowlist ("whitelist") control, i.e. step 3 becomes "blocked"
for some rooms or schedules (exam mode, primary-school labs). The order above already
supports it; it needs per-policy modes in the sinkhole (Site-blocker/readme.md, section 4).
"""  # ruff: ignore[line-too-long]

from __future__ import annotations

import ipaddress
import re
from contextlib import suppress
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

# One DNS label: letters, digits, hyphen (not at either end), underscore.
# (seen in real blocklists, e.g. "_dmarc" style names); 1-63 characters.
_LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")
MAX_DOMAIN = 253


def normalize_domain(raw: str) -> str | None:
    """Well-formed, lowercased, dot-less-at-the-end domain name in ASCII (IDN as punycode, which is what DNS queries carry), or None.

    Rejected: empty, IP addresses, single labels (localhost, ip6-localhost, local,
    broadcasthost: names that only mean something on the machine itself), wildcards,
    empty or over-long labels, labels starting/ending with "-", anything over 253 chars.
    Public suffixes are a separate check (is_public_suffix).

    Returns:
        str | None: domain or None

    """  # ruff: ignore[line-too-long]
    domain = raw.strip().lower().rstrip(".")
    if not domain or len(domain) > MAX_DOMAIN:
        return None

    with suppress(ValueError):
        ipaddress.ip_address(domain)
        return None

    if not domain.isascii():
        try:
            # Recode non-ascii: ünnep.hu -> xn--nnep-1sa.hu
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            return None

    labels = domain.split(".")
    if len(labels) < 2 or len(domain) > MAX_DOMAIN:  # ruff: ignore[magic-value-comparison]
        return None
    if not all(_LABEL.match(label) for label in labels):
        return None

    return domain


@cache
def _extractor():  # ruff: ignore[missing-return-type-private-function]
    import tldextract  # ruff: ignore[import-outside-top-level]

    # suffix_list_urls=(): the bundled snapshot only, never a network fetch;
    # cache_dir=None: nothing written to disk (the server runs as an unprivileged user).  # ruff: ignore[line-too-long]
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=False)  # ruff: ignore[line-too-long]


def is_public_suffix(domain: str) -> bool:
    """True for com, hu, co.uk, co.hu, ... (also wildcard rules such as *.ck)."""  # ruff: ignore[docstring-missing-returns, line-too-long, non-imperative-mood]
    r = _extractor()(domain)
    return bool(r.suffix) and not r.domain and not r.subdomain


@cache
def plain_public_suffixes() -> tuple[str, ...]:
    """Get the suffixes themselves (com, co.uk, ...), without the list's wildcard and exception rules: what a legacy blocklist row could be (the sync leaves those out in SQL)."""  # ruff: ignore[docstring-missing-returns, line-too-long]
    return tuple(sorted(t for t in _extractor().tlds if not t.startswith(("*", "!"))))  # ruff: ignore[line-too-long]


def public_suffix_count() -> int:  # ruff: ignore[undocumented-public-function]
    return len(_extractor().tlds)


def public_suffix_source() -> str:  # ruff: ignore[undocumented-public-function]
    import tldextract  # ruff: ignore[import-outside-top-level]

    return f"Public Suffix List (ICANN section, tldextract {tldextract.__version__} bundled snapshot)"  # ruff: ignore[line-too-long]


class _Entry(Protocol):
    domain: str
    include_subdomains: bool


# The built-in part of the blocklist: public DNS-over-HTTPS servers and Apple's iCloud  # ruff: ignore[line-too-long]
# Private Relay. A browser or phone using one of them resolves names without asking  # ruff: ignore[line-too-long]
# SMP's DNS, so nothing else on the blocklist would apply to it. Always blocked with  # ruff: ignore[line-too-long]
# their subdomains, unless an allowlist entry covers them (a school that wants one).  # ruff: ignore[line-too-long]
# The sinkhole has the same list built in (defaultPrivateDNSDomains in
# Site-blocker/school-dns-demo/plugin/schoolfilter/blocklist.go), so it blocks them even  # ruff: ignore[line-too-long]
# before its first sync; test_units.py keeps the two lists equal.
BUILTIN_BLOCKLIST: tuple[str, ...] = (
    "cloudflare-dns.com",
    "dns.adguard-dns.com",
    "dns.adguard.com",
    "dns.google",
    "dns.nextdns.io",
    "dns.quad9.net",
    "doh.cleanbrowsing.org",
    "doh.opendns.com",
    "mask-h2.icloud.com",
    "mask.icloud.com",
    "mozilla.cloudflare-dns.com",
    "one.one.one.one",
)


@dataclass(frozen=True, slots=True)
class BuiltinBlock:  # ruff: ignore[undocumented-public-class]
    domain: str
    include_subdomains: bool = True


_BUILTIN: Mapping[str, BuiltinBlock] = {d: BuiltinBlock(d) for d in BUILTIN_BLOCKLIST}  # ruff: ignore[line-too-long]


def match(domain: str, entries: Mapping[str, _Entry]) -> _Entry | None:
    """Get the entry covering `domain`: itself, or the nearest parent with include_subdomains."""  # ruff: ignore[docstring-missing-returns, line-too-long]
    if (e := entries.get(domain)) is not None:
        return e
    labels = domain.split(".")
    for i in range(1, len(labels)):
        e = entries.get(".".join(labels[i:]))
        if e is not None and e.include_subdomains:
            return e
    return None


def blocked_by(domain: str, allow: Mapping[str, _Entry], block: Mapping[str, _Entry]) -> _Entry | None:  # ruff: ignore[line-too-long]
    """Get the entry that blocks `domain` (a BuiltinBlock for the built-in part), applying the decision order above."""  # ruff: ignore[docstring-missing-returns, line-too-long]
    if is_public_suffix(domain) or match(domain, allow) is not None:
        return None
    if (builtin := match(domain, _BUILTIN)) is not None:  # pyright: ignore[reportArgumentType]
        return builtin
    entry = match(domain, block)
    if entry is not None and is_public_suffix(entry.domain):
        return None  # legacy entry for a whole TLD: never applied
    return entry


def by_domain(entries: Iterable[_Entry]) -> dict[str, _Entry]:  # ruff: ignore[undocumented-public-function]
    return {e.domain: e for e in entries}


# Names hosts files list for the machine itself (StevenBlack's header has them), not sites.  # ruff: ignore[line-too-long]
_HOSTS_SYSTEM_NAMES = frozenset({
    "localhost.localdomain",
    "localhost6.localdomain6",
    "ip6-localhost.localdomain"})

# A "#" starts a comment only at the start of a line or after whitespace:
# "0.0.0.0 ads.example # tracker" is a comment, "example.com##.banner" is not.
_COMMENT = re.compile(r"(^|\s)#.*$")
# Browser ad-blocker rules that hide parts of a page (element hiding, scriptlets:  # ruff: ignore[line-too-long]
# "example.com##.banner", "example.com#@#.ad", "#?#", "#$#", "#%#", ...). They name the  # ruff: ignore[line-too-long]
# site the rule works on, not a site to block, so the whole line is skipped.
# TODO(evaluate): such per-site rules could become something other than a DNS block.  # ruff: ignore[line-too-long]
_COSMETIC = re.compile(r"#@?[$?%]{0,2}#")


def _is_badfilter(rule: str) -> bool:
    """`||example.com^$badfilter` withdraws the list's own rule for example.com;

    It never means "block".
    """  # ruff: ignore[docstring-missing-returns, line-too-long, missing-terminal-punctuation, missing-trailing-period]
    _, sep, modifiers = rule.partition("$")
    return bool(sep) and "badfilter" in (m.strip().lower() for m in modifiers.split(","))  # ruff: ignore[line-too-long]


def parse_hosts(content: bytes, progress: Callable[[int, int], None] | None = None) -> list[str]:  # ruff: ignore[complex-structure, line-too-long, too-many-branches]
    """Domains to block from hosts files and plain/adblock domain lists, in file order,
    without duplicates. Hosts mappings must use a block or normal unicast address.
    Comments, allow rules (@@), withdrawn rules ($badfilter), element-hiding rules
    (example.com##...), system entries and malformed names are skipped.

    `progress(lines_done, lines_total)` is called every few thousand lines (imports run
    this in a worker thread and show how far it got).
    """  # ruff: ignore[docstring-missing-returns, line-too-long, missing-blank-line-after-summary]
    found: list[str] = []
    seen: set[str] = set()
    lines = content.decode("utf-8", errors="replace").removeprefix("\ufeff").splitlines()  # ruff: ignore[line-too-long]
    for number, raw_line in enumerate(lines):
        if progress is not None and number % 5000 == 0:
            progress(number, len(lines))
        line = _COMMENT.sub("", raw_line).strip()
        if not line or line.startswith("!") or _COSMETIC.search(line):
            continue
        tokens = line.split()
        first_domain = 0
        last_domain = len(tokens)
        try:
            ip = ipaddress.ip_address(tokens[0])
        except ValueError:
            if tokens[0].startswith("@@"):
                continue
            last_domain = 1
        else:
            if str(ip) not in {"0.0.0.0", "127.0.0.1"} and (  # ruff: ignore[hardcoded-bind-all-interfaces, too-many-boolean-expressions]
                ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_reserved or ip.is_link_local  # ruff: ignore[line-too-long]
            ):
                continue
            first_domain = 1
        for token in tokens[first_domain:last_domain]:
            if token.startswith("@@") or _is_badfilter(token):
                continue
            rule = token.lower().strip()
            if rule.startswith("||"):
                rule = rule[2:]
            elif rule.startswith("|"):
                rule = rule[1:]
            rule = rule.split("$", 1)[0].split("^", 1)[0]
            domain = normalize_domain(rule)
            if domain and domain not in seen and domain not in _HOSTS_SYSTEM_NAMES:  # ruff: ignore[line-too-long]
                seen.add(domain)
                found.append(domain)
    if progress is not None:
        progress(len(lines), len(lines))
    return found
