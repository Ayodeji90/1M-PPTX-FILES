"""robots.txt compliance: fetch + cache per domain (SQLite, 24h TTL).

Checked before first contact with a domain and before every download.
The verdict per file is recorded in the audit record (contractual).
Honors Disallow and Crawl-delay for our specific UA and for '*'.
"""
from __future__ import annotations

import logging
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from ..db.dao import Registry, utcnow

log = logging.getLogger("pptxsweeper.net.robots")

_UA_TOKEN = "PptxSweeperBot"   # robots.txt matches on the product token


@dataclass
class RobotsVerdict:
    allowed: bool
    status: str          # allowed | disallowed | unavailable
    crawl_delay: float | None = None


class RobotsCache:
    """Per-domain robots.txt cache backed by the `domains` table."""

    def __init__(self, reg: Registry, fetcher, ttl_hours: int = 24):
        """`fetcher` is an async callable (url) -> (status_code, text|None)."""
        self.reg = reg
        self.fetcher = fetcher
        self.ttl = timedelta(hours=ttl_hours)

    async def verdict(self, url: str) -> RobotsVerdict:
        domain = urlsplit(url).netloc.lower()
        robots_txt = await self._get_robots_txt(domain, urlsplit(url).scheme or "https")
        if robots_txt is None:
            # Could not fetch robots.txt (network error). Conservative
            # default: allow (standard practice when robots is absent),
            # but record 'unavailable' in the audit trail.
            return RobotsVerdict(allowed=True, status="unavailable")

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(robots_txt.splitlines())
        allowed = parser.can_fetch(_UA_TOKEN, url)
        delay = parser.crawl_delay(_UA_TOKEN)
        if delay is None:
            delay = parser.crawl_delay("*")
        return RobotsVerdict(
            allowed=allowed,
            status="allowed" if allowed else "disallowed",
            crawl_delay=float(delay) if delay is not None else None,
        )

    async def _get_robots_txt(self, domain: str, scheme: str) -> str | None:
        row = self.reg.get_domain(domain)
        if row and row["robots_fetched_at"]:
            fetched_at = datetime.fromisoformat(row["robots_fetched_at"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - fetched_at < self.ttl:
                return row["robots_cache"]  # may be "" (=absent robots)

        status, text = await self.fetcher(f"{scheme}://{domain}/robots.txt")
        if status is None:
            # network failure: reuse stale cache if any, else unavailable
            return row["robots_cache"] if row and row["robots_cache"] is not None else None
        if status >= 500:
            # Per RFC 9309: 5xx == assume disallow-all until reachable.
            text = "User-agent: *\nDisallow: /"
        elif status >= 400 or text is None:
            # 4xx == no robots restrictions.
            text = ""

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(text.splitlines())
        delay = parser.crawl_delay(_UA_TOKEN) or parser.crawl_delay("*")
        self.reg.upsert_domain(
            domain,
            robots_cache=text,
            robots_fetched_at=utcnow(),
            crawl_delay=float(delay) if delay is not None else None,
        )
        return text
