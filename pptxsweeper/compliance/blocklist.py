"""Pirate/exclusion domain blocklist: config file of domains + patterns.

File format (seeds/blocklist_domains.txt): one entry per line,
`#` comments. An entry is either a bare domain (blocks it and all
subdomains) or a regex prefixed with `re:` matched against full URLs.
Hits are hard rejects.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("pptxsweeper.compliance.blocklist")


class Blocklist:
    def __init__(self, domains: set[str], patterns: list[re.Pattern]):
        self.domains = domains
        self.patterns = patterns

    @classmethod
    def load(cls, path: str | Path) -> "Blocklist":
        domains: set[str] = set()
        patterns: list[re.Pattern] = []
        path = Path(path)
        if not path.exists():
            log.warning("blocklist file %s missing; blocklist is EMPTY", path)
            return cls(domains, patterns)
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = line.split("#", 1)[0].strip().lower()
            if not entry:
                continue
            if entry.startswith("re:"):
                try:
                    patterns.append(re.compile(entry[3:], re.IGNORECASE))
                except re.error as exc:
                    log.error("bad blocklist regex %r: %s", entry, exc)
            else:
                domains.add(entry.lstrip("."))
        log.info("blocklist loaded: %d domains, %d patterns", len(domains), len(patterns))
        return cls(domains, patterns)

    def blocked(self, url: str) -> str | None:
        """Return the matching entry if blocked, else None."""
        host = urlsplit(url).netloc.lower().split(":")[0]
        parts = host.split(".")
        for i in range(len(parts) - 1):
            candidate = ".".join(parts[i:])
            if candidate in self.domains:
                return candidate
        for pattern in self.patterns:
            if pattern.search(url):
                return f"re:{pattern.pattern}"
        return None
