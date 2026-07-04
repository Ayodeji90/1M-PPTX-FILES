"""Per-domain politeness: token-bucket delays with jitter.

Default 1 request / 3s per domain, ±40% random jitter; a larger
robots.txt Crawl-delay always wins. Domains are worker-sharded so only
one worker ever touches a domain -- this limiter is the belt to that
suspender (it still serializes if sharding is misconfigured).
"""
from __future__ import annotations

import asyncio
import random
import time


class DomainRateLimiter:
    def __init__(self, default_delay_s: float = 1.5, jitter_pct: float = 0.4,
                 overrides: dict[str, float] | None = None):
        self.default_delay_s = default_delay_s
        self.jitter_pct = jitter_pct
        self.overrides = {k.lower(): float(v) for k, v in (overrides or {}).items()}
        self._next_allowed: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._crawl_delays: dict[str, float] = {}

    def set_crawl_delay(self, domain: str, delay_s: float | None) -> None:
        if delay_s is not None:
            self._crawl_delays[domain] = float(delay_s)

    def _base_delay(self, domain: str) -> float:
        domain = domain.lower()
        for suffix, delay in self.overrides.items():
            if domain == suffix or domain.endswith("." + suffix):
                return delay
        return self.default_delay_s

    def _delay_for(self, domain: str) -> float:
        base = max(self._base_delay(domain), self._crawl_delays.get(domain, 0.0))
        jitter = base * self.jitter_pct
        return base + random.uniform(-jitter, jitter)

    async def wait(self, domain: str) -> None:
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:  # strict serialism within a domain
            now = time.monotonic()
            next_allowed = self._next_allowed.get(domain, 0.0)
            if next_allowed > now:
                await asyncio.sleep(next_allowed - now)
            self._next_allowed[domain] = time.monotonic() + self._delay_for(domain)
