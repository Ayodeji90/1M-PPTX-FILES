"""Wayback Machine fallback fetcher.

When an origin URL is dead/blocked (parked or blacklisted domain, 404,
network-dead), retrieve the archived copy instead. Politeness: <=1
req/sec per worker, honor 429 with backoff.

`id_` flag on the timestamp URL returns the original bytes without
Wayback's rewriting -- required for binary payloads.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("pptxsweeper.net.wayback")


class WaybackFetcher:
    def __init__(self, client: httpx.AsyncClient,
                 fetch_base_url: str = "https://web.archive.org",
                 requests_per_sec: float = 1.0,
                 retry_after_429_s: tuple[float, ...] = (30, 120, 300)):
        self.client = client
        self.base = fetch_base_url.rstrip("/")
        self.min_interval = 1.0 / requests_per_sec
        self.retry_after_429_s = retry_after_429_s
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def _polite_get(self, url: str, **kwargs) -> httpx.Response:
        async with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
        return await self.client.get(url, **kwargs)

    async def find_snapshot(self, url: str) -> str | None:
        """Most recent snapshot timestamp for `url`, or None."""
        api = f"{self.base}/wayback/available"
        for attempt, backoff in enumerate((0, *self.retry_after_429_s)):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                resp = await self._polite_get(api, params={"url": url})
            except httpx.HTTPError as exc:
                log.debug("wayback availability check failed for %s: %s", url, exc)
                return None
            if resp.status_code == 429:
                continue
            if resp.status_code != 200:
                return None
            snap = (resp.json().get("archived_snapshots") or {}).get("closest") or {}
            if snap.get("available") and snap.get("timestamp"):
                return snap["timestamp"]
            return None
        return None

    async def fetch(self, url: str, timestamp: str | None = None
                    ) -> tuple[bytes | None, str | None, int | None]:
        """Fetch the archived copy of `url`.
        Returns (payload_bytes, snapshot_url, http_status). Payload is None
        when no snapshot exists or fetching failed. A known snapshot
        `timestamp` (e.g. recorded at CDX-discovery time) skips the flaky
        availability API."""
        if not timestamp:
            timestamp = await self.find_snapshot(url)
        if not timestamp:
            return None, None, None
        snapshot_url = f"{self.base}/web/{timestamp}id_/{url}"
        for attempt, backoff in enumerate((0, *self.retry_after_429_s)):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                resp = await self._polite_get(snapshot_url)
            except httpx.HTTPError as exc:
                log.debug("wayback fetch failed for %s: %s", snapshot_url, exc)
                return None, snapshot_url, None
            if resp.status_code == 429:
                continue
            if resp.status_code == 200:
                return resp.content, snapshot_url, 200
            return None, snapshot_url, resp.status_code
        return None, snapshot_url, 429
