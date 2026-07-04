"""Tier 1: Common Crawl index harvester.

Verified against the live endpoints (2026-07):

- Anonymous S3 access to s3://commoncrawl is DENIED. The supported free
  path is HTTPS via https://data.commoncrawl.org. Each crawl publishes
  `crawl-data/{CRAWL}/cc-index-table.paths.gz` listing the ~300 parquet
  part files of the columnar index; we range-read those over HTTPS with
  pyarrow (only the `url` + `fetch_status` columns are fetched).
- The CDX API CANNOT answer "all URLs ending .pptx": indexes are keyed
  by domain (SURT), and only exact `filter=mime:...` filters work
  (regex url filters return "No Captures found"). So the CDX fallback
  is DOMAIN-SCOPED: it queries each seed/known domain for PowerPoint
  mime types. Suffix discovery at web scale needs the parquet path.

Resumable: completed parquet parts are recorded in `harvest_cursor`, so
a restart skips them. Multi-node: parts are sharded by
part_index % NODE_COUNT == NODE_ID.

CC WARC payloads are truncated at ~1MB -- CC is strictly a URL/metadata
index here, never a file source.
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import time
from collections.abc import AsyncIterator

import httpx

from ..node import NodeIdentity
from .base import CandidateURL, Harvester, register

log = logging.getLogger("pptxsweeper.harvest.cc")

PPT_MIMES = (
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
)


class HttpRangeFile(io.RawIOBase):
    """Random-access file over HTTP Range requests (for pyarrow)."""

    def __init__(self, client: httpx.Client, url: str, retries: int = 4):
        super().__init__()
        self.client = client
        self.url = url
        self.retries = retries
        head = self._request("HEAD")
        self._size = int(head.headers["content-length"])
        self._pos = 0

    def _request(self, method: str, **kwargs) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self.client.request(method, self.url, **kwargs)
                if resp.status_code in (200, 206):
                    return resp
                last = RuntimeError(f"HTTP {resp.status_code} for {self.url}")
            except httpx.HTTPError as exc:
                last = exc
            time.sleep(min(2 ** attempt, 15))
        raise last if last else RuntimeError("unreachable")

    # -- file protocol ---------------------------------------------------
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._size + offset
        return self._pos

    def size(self) -> int:
        return self._size

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._size - self._pos
        if n <= 0 or self._pos >= self._size:
            return b""
        end = min(self._pos + n, self._size) - 1
        resp = self._request("GET", headers={"Range": f"bytes={self._pos}-{end}"})
        data = resp.content
        self._pos += len(data)
        return data


@register
class CommonCrawlHarvester(Harvester):
    name = "commoncrawl"
    tier = 1

    async def discover(self) -> AsyncIterator[CandidateURL]:
        cc = self.cfg.raw["common_crawl"]
        node = NodeIdentity.from_env()
        crawls = await self._recent_crawls(int(cc["num_recent_crawls"]))
        if not crawls:
            log.error("could not list Common Crawl crawls (collinfo.json)")
            return

        for crawl_id in crawls:
            parts = await self._warc_parquet_parts(crawl_id)
            if parts:
                async for candidate in self._discover_parquet(crawl_id, parts, node):
                    yield candidate
            else:
                log.warning(
                    "crawl %s: parquet manifest unavailable; falling back to "
                    "domain-scoped CDX (suffix search needs the parquet index)",
                    crawl_id)
                async for candidate in self._discover_cdx(crawl_id):
                    yield candidate

    # ------------------------------------------------------------------
    async def _recent_crawls(self, n: int) -> list[str]:
        resp = await self.polite_get(self.cfg.raw["common_crawl"]["index_list_url"])
        if resp is None or resp.status_code != 200:
            return []
        return [c["id"] for c in resp.json()][:n]

    async def _warc_parquet_parts(self, crawl_id: str) -> list[str]:
        base = self.cfg.raw["common_crawl"]["data_base_url"].rstrip("/")
        manifest_url = f"{base}/crawl-data/{crawl_id}/cc-index-table.paths.gz"
        resp = await self.polite_get(manifest_url, delay_s=1.0)
        if resp is None or resp.status_code != 200:
            return []
        try:
            text = gzip.decompress(resp.content).decode("utf-8", errors="replace")
        except OSError:
            text = resp.text
        return [line.strip() for line in text.splitlines()
                if "/subset=warc/" in line and line.strip().endswith(".parquet")]

    # ------------------------------------------------------------------
    # Parquet over HTTPS (primary)
    # ------------------------------------------------------------------
    def _cursor_done(self, key: str) -> bool:
        return self._reg().conn.execute(
            "SELECT 1 FROM harvest_cursor WHERE source=?", (key,)).fetchone() is not None

    def _cursor_mark(self, key: str, detail: str = "") -> None:
        reg = self._reg()
        with reg.tx():
            reg.conn.execute(
                "INSERT OR REPLACE INTO harvest_cursor (source, cursor) VALUES (?,?)",
                (key, detail))

    def _reg(self):
        # The registry connection is attached by run_harvesters via cfg;
        # fall back to opening one (harvest is a single-process stage).
        if not hasattr(self, "_registry"):
            from ..db.dao import Registry
            self._registry = Registry(self.cfg.path("paths", "db_path"))
        return self._registry

    async def _discover_parquet(self, crawl_id: str, parts: list[str],
                                node: NodeIdentity) -> AsyncIterator[CandidateURL]:
        import asyncio
        base = self.cfg.raw["common_crawl"]["data_base_url"].rstrip("/")
        extensions = tuple(e.lower() for e in self.cfg.raw["common_crawl"]["extensions"])
        my_parts = [(i, p) for i, p in enumerate(sorted(parts))
                    if i % node.node_count == node.node_id]
        log.info("crawl %s: %d parquet parts (%d owned by this node)",
                 crawl_id, len(parts), len(my_parts))

        for index, part in my_parts:
            key = f"cc:{crawl_id}:{part}"
            if self._cursor_done(key):
                continue
            url = f"{base}/{part}"
            found = 0
            try:
                for candidate_url, status in await asyncio.to_thread(
                        self._scan_parquet_part, url, extensions):
                    found += 1
                    yield CandidateURL(
                        url=candidate_url, tier=self.tier,
                        discovery_source=f"commoncrawl:{crawl_id}",
                        metadata={"cc_fetch_status": status, "cc_part": index},
                    )
            except Exception as exc:
                log.warning("crawl %s part %d failed (%s); will retry next run",
                            crawl_id, index, exc)
                continue
            self._cursor_mark(key, f"found={found}")
            log.info("crawl %s part %d/%d done: %d presentation URLs",
                     crawl_id, index + 1, len(parts), found)

    def _scan_parquet_part(self, url: str, extensions: tuple[str, ...]
                           ) -> list[tuple[str, int | None]]:
        """Blocking: range-read one parquet part, return matching rows."""
        import pyarrow.compute as pc
        import pyarrow.parquet as pq

        results: list[tuple[str, int | None]] = []
        with httpx.Client(timeout=httpx.Timeout(60, connect=20),
                          headers={"User-Agent": self.cfg.user_agent}) as client:
            f = HttpRangeFile(client, url)
            pf = pq.ParquetFile(f, pre_buffer=True)
            for batch in pf.iter_batches(batch_size=1 << 16,
                                         columns=["url", "fetch_status"]):
                urls = pc.utf8_lower(batch.column("url"))
                mask = None
                for ext in extensions:
                    cond = pc.ends_with(urls, pattern=ext)
                    mask = cond if mask is None else pc.or_(mask, cond)
                if mask is None or pc.sum(mask).as_py() in (0, None):
                    continue
                filtered = batch.filter(mask)
                for row in filtered.to_pylist():
                    if row.get("fetch_status") in (200, None):
                        results.append((row["url"], row.get("fetch_status")))
        return results

    # ------------------------------------------------------------------
    # Domain-scoped CDX fallback
    # ------------------------------------------------------------------
    async def _discover_cdx(self, crawl_id: str) -> AsyncIterator[CandidateURL]:
        from .seeds_util import tier1_seed_domains
        base = self.cfg.raw["common_crawl"]["cdx_fallback_base"].rstrip("/")
        api = f"{base}/{crawl_id}-index"
        domains = tier1_seed_domains(self.cfg, self._reg())
        log.info("crawl %s: CDX fallback across %d seed domains", crawl_id, len(domains))
        for domain in domains:
            for mime in PPT_MIMES:
                page = 0
                while True:
                    resp = await self.polite_get(api, params={
                        "url": domain, "matchType": "domain",
                        "filter": f"mime:{mime}",
                        "output": "json", "page": page,
                    }, delay_s=1.5, retries=2)
                    if resp is None or resp.status_code != 200 or not resp.text.strip():
                        break
                    got = 0
                    for line in resp.text.splitlines():
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get("status") not in ("200", 200):
                            continue
                        got += 1
                        yield CandidateURL(
                            url=rec.get("url", ""), tier=self.tier,
                            discovery_source=f"commoncrawl-cdx:{crawl_id}",
                            metadata={"cc_mime": rec.get("mime"),
                                      "cc_length": rec.get("length")},
                        )
                    if got == 0:
                        break
                    page += 1
