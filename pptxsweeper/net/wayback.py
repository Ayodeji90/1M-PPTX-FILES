"""Wayback Machine fallback fetcher.

When an origin URL is dead/blocked (parked or blacklisted domain, 404,
network-dead), retrieve the archived copy instead. Politeness: <=1
req/sec per worker, honor 429 with backoff.

`id_` flag on the timestamp URL returns the original bytes without
Wayback's rewriting -- required for binary payloads.

Memory: `fetch_to_file` STREAMS the archived body straight to disk
(computing SHA256 on the fly) instead of buffering the whole payload in
RAM. With up to 300 MB allowed per file and many workers, an in-memory
`fetch()` (resp.content) could spike multi-GB of RAM; the streaming path
keeps the downloader's memory flat regardless of file size, so it is the
only fetch path the pipeline uses.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from ..utils.hashing import StreamingSha256

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

    async def fetch_to_file(self, url: str, dest: Path,
                            timestamp: str | None = None,
                            max_bytes: int | None = None,
                            min_bytes: int = 0,
                            ) -> tuple[bool, str | None, int, str | None, int | None]:
        """STREAM the archived copy of `url` to `dest` on disk.

        Returns (ok, sha256, size, snapshot_url, http_status):
          ok=True           body streamed to `dest`; sha256/size valid.
          ok=False, size>0  body fetched but rejected by the size gates
                            (oversize -> dest deleted; undersize -> dest
                            deleted); caller should mark the URL rejected.
          ok=False, size=0  no snapshot / fetch failed; caller marks dead.
        A known snapshot `timestamp` skips the availability API.
        """
        if not timestamp:
            timestamp = await self.find_snapshot(url)
        if not timestamp:
            return False, None, 0, None, None
        snapshot_url = f"{self.base}/web/{timestamp}id_/{url}"
        for attempt, backoff in enumerate((0, *self.retry_after_429_s)):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                async with self.client.stream("GET", snapshot_url) as resp:
                    if resp.status_code == 429:
                        continue
                    if resp.status_code != 200:
                        return False, None, 0, snapshot_url, resp.status_code
                    hasher = StreamingSha256()
                    size = 0
                    oversize = False
                    with open(dest, "wb") as fh:
                        async for chunk in resp.aiter_bytes(chunk_size=1 << 16):
                            size += len(chunk)
                            if max_bytes and size > max_bytes:
                                oversize = True
                                break
                            hasher.update(chunk)
                            fh.write(chunk)
                    if oversize:
                        dest.unlink(missing_ok=True)
                        return False, None, size, snapshot_url, 200
                    if size < min_bytes:
                        dest.unlink(missing_ok=True)
                        return False, None, size, snapshot_url, 200
                    return True, hasher.hexdigest(), size, snapshot_url, 200
            except httpx.HTTPError as exc:
                log.debug("wayback stream failed for %s: %s", snapshot_url, exc)
                dest.unlink(missing_ok=True)
                return False, None, 0, snapshot_url, None
        return False, None, 0, snapshot_url, 429
