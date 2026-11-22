# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Domain rules for the DNS module: validation, public suffixes, matching, hosts files.

Decision order for a query (the Go schoolfilter implements the same in Match):

    1. allowlist      -> allowed   (user entries; exact, or a parent with include_subdomains)
    2. blocklist      -> blocked
    3. anything else  -> allowed

Public suffixes (com, co.uk, hu, ...) are a built-in part of the allowlist: they can
never be put on the blocklist or the allowlist, because one such entry would block
(or unblock) a whole top-level domain for the school. They come from the Public
Suffix List bundled with `tldextract` (its offline snapshot, ICANN section only; the
server never downloads it). Updating the `tldextract` package updates the list.

TODO(evaluate, issue #3): total allowlist ("whitelist") control, i.e. step 3 becomes "blocked"
for some rooms or schedules (exam mode, primary-school labs). The order above already
supports it; it needs per-policy modes in the sinkhole (Site-blocker/readme.md, section 4).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from functools import cache
from typing import Protocol

# One DNS label: letters, digits, hyphen (not at either end), underscore (seen in
# real blocklists, e.g. "_dmarc" style names); 1-63 characters.
_LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")
MAX_DOMAIN = 253


def normalize_domain(raw: str) -> str | None:
    """A well-formed, lowercased, dot-less-at-the-end domain name in ASCII (IDN as
    punycode, which is what DNS queries carry), or None.

    Rejected: empty, IP addresses, single labels (localhost, ip6-localhost, local,
    broadcasthost: names that only mean something on the machine itself), wildcards,
    empty or over-long labels, labels starting/ending with "-", anything over 253 chars.
    Public suffixes are a separate check (is_public_suffix)."""
    domain = raw.strip().lower().rstrip(".")
    if not domain or len(domain) > MAX_DOMAIN:
        return None
    try:
        ipaddress.ip_address(domain)
        return None
    except ValueError:
        pass
    if not domain.isascii():
        try:
            domain = domain.encode("idna").decode("ascii")  # ünnep.hu -> xn--nnep-1sa.hu
        except UnicodeError:
            return None
    labels = domain.split(".")
    if len(labels) < 2 or len(domain) > MAX_DOMAIN:
        return None
    if not all(_LABEL.match(label) for label in labels):
        return None
    return domain


@cache
def _extractor():
    import tldextract

    # suffix_list_urls=(): the bundled snapshot only, never a network fetch;
    # cache_dir=None: nothing written to disk (the server runs as an unprivileged user).
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=False)


def is_public_suffix(domain: str) -> bool:
    """True for com, hu, co.uk, co.hu, ... (also wildcard rules such as *.ck)."""
    r = _extractor()(domain)
    return bool(r.suffix) and not r.domain and not r.subdomain


def public_suffix_count() -> int:
    return len(_extractor().tlds)


def public_suffix_source() -> str:
    import tldextract

    return f"Public Suffix List (ICANN section, tldextract {tldextract.__version__} bundled snapshot)"


class _Entry(Protocol):
    domain: str
    include_subdomains: bool


def match(domain: str, entries: Mapping[str, _Entry]) -> _Entry | None:
    """The entry covering `domain`: itself, or the nearest parent with include_subdomains."""
    if (e := entries.get(domain)) is not None:
        return e
    labels = domain.split(".")
    for i in range(1, len(labels)):
        e = entries.get(".".join(labels[i:]))
        if e is not None and e.include_subdomains:
            return e
    return None


def blocked_by(domain: str, allow: Mapping[str, _Entry], block: Mapping[str, _Entry]) -> _Entry | None:
    """The blocklist entry that blocks `domain`, applying the decision order above."""
    if is_public_suffix(domain) or match(domain, allow) is not None:
        return None
    entry = match(domain, block)
    if entry is not None and is_public_suffix(entry.domain):
        return None  # legacy entry for a whole TLD: never applied
    return entry


def by_domain(entries: Iterable[_Entry]) -> dict[str, _Entry]:
    return {e.domain: e for e in entries}


def parse_hosts(content: bytes) -> list[str]:
    """Domains to block from a hosts file (e.g. StevenBlack's), in file order, without
    duplicates. Only lines mapping a domain to a sink address count: 0.0.0.0 or
    127.0.0.1 (the usual "block" targets), or any normal unicast address. Comments,
    the file's own system entries (localhost, ::1, fe80::/ff02:: names, broadcasthost)
    and malformed names are skipped."""
    found: list[str] = []
    seen: set[str] = set()
    for raw_line in content.decode("utf-8", errors="replace").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        try:
            ip = ipaddress.ip_address(tokens[0])
        except ValueError:
            continue
        if str(ip) not in {"0.0.0.0", "127.0.0.1"} and (
            ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_reserved or ip.is_link_local
        ):
            continue
        for token in tokens[1:]:
            domain = normalize_domain(token)
            if domain and domain not in seen:
                seen.add(domain)
                found.append(domain)
    return found
