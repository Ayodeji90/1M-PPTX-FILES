"""Async download stage.

Design (spec-mandated):
- 100-200 async workers; global parallelism across domains, strict
  serialism within a domain (domains are sharded to exactly one worker).
- robots.txt checked before every download; verdict recorded per URL.
- HEAD-before-GET where supported (content-type / length gates).
- Streams to local temp computing SHA256 on the fly; known-hash
  duplicates dropped immediately.
- Circuit breakers per domain; parked/blacklisted/dead URLs route to the
  Wayback fallback fetcher.
- All DB writes through the single-writer task (writer.py).
- Resumable: rows stuck in 'downloading' from a crash are reclaimed at
  startup; a killed process loses nothing.
- Graceful SIGTERM: finish in-flight file, commit, exit.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from urllib.parse import urlsplit, unquote

import httpx

from ..config import Config
from ..db.dao import Registry, utcnow
from ..node import NodeIdentity
from ..net.breaker import CircuitBreaker, RETRYABLE_STATUS
from ..net.client import build_client, fetch_text
from ..net.ratelimit import DomainRateLimiter
from ..net.robots import RobotsCache
from ..net.wayback import WaybackFetcher
from ..utils.disk import has_free_space
from ..utils.hashing import StreamingSha256
from .validate import sniff_format

log = logging.getLogger("pptxsweeper.download")

_DOC_CONTENT_TYPES = (
    "application/vnd.openxmlformats-officedocument.presentationml",
    "application/vnd.ms-powerpoint",
    "application/pdf",
    "application/octet-stream",
    "binary/octet-stream",
    "application/zip",
    "application/x-zip",
    "application/download",
    "application/force-download",
)


def _filename_from_url(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "unnamed"
    return name[:255]


class DownloadStage:
    def __init__(self, cfg: Config, reg: Registry, node: NodeIdentity | None = None,
                 dry_run: bool = False, concurrency: int | None = None):
        self.cfg = cfg
        self.reg = reg
        self.node = node or NodeIdentity.from_env()
        self.dry_run = dry_run
        dl = cfg.raw["download"]
        self.concurrency = concurrency or int(dl["concurrency"])
        self.claim_batch_size = int(dl["claim_batch_size"])
        self.max_attempts = int(dl["max_attempts"])
        self.max_bytes = int(dl["max_content_length_mb"]) * 1024 * 1024
        self.min_bytes = int(dl["min_content_length_kb"]) * 1024
        self.min_free_gb = float(dl["min_free_disk_gb"])
        self.head_before_get = bool(cfg.raw["politeness"]["head_before_get"])
        self.wayback_enabled = bool(dl.get("wayback_fallback", True))
        self.tmp_dir = cfg.path("paths", "download_tmp_dir")
        self.stop_event = asyncio.Event()

        from .writer import DbWriter
        self.writer = DbWriter(reg, batch_size=int(dl["db_writer_batch_size"]),
                               flush_interval_s=float(dl["db_writer_flush_interval_s"]))
        self.limiter = DomainRateLimiter(
            default_delay_s=float(cfg.raw["politeness"]["default_delay_s"]),
            jitter_pct=float(cfg.raw["politeness"]["jitter_pct"]),
            overrides=dict(cfg.raw["politeness"].get("domain_delay_overrides") or {}),
        )
        cb = cfg.raw["politeness"]["circuit_breaker"]
        self.breaker = CircuitBreaker(
            reg=reg,
            backoff_stages_s=tuple(cb["backoff_stages_s"]),
            park_after=int(cb["park_after_consecutive_failures"]),
            park_duration_hours=float(cb["park_duration_hours"]),
            blacklist_after_parks=int(cb["blacklist_after_parks"]),
        )
        self.client: httpx.AsyncClient | None = None
        self.wayback: WaybackFetcher | None = None
        self.robots: RobotsCache | None = None
        self.stats = {"downloaded": 0, "duplicates": 0, "dead": 0, "rejected": 0,
                      "requeued": 0, "wayback": 0}

    # ------------------------------------------------------------------
    async def run(self) -> dict:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._install_signal_handlers()
        self._cleanup_stale_parts()
        self._recover_stale_claims()

        domains = self.node.filter_domains(self.reg.distinct_domains("discovered"))
        if not domains:
            log.info("no domains with discovered URLs for this node; nothing to do")
            return self.stats
        shards = self._shard_domains(domains)
        log.info("downloading from %d domains across %d workers (node %d/%d)%s",
                 len(domains), len(shards), self.node.node_id, self.node.node_count,
                 " [DRY RUN]" if self.dry_run else "")

        self.client = build_client(
            self.cfg.user_agent,
            connect_timeout=float(self.cfg.raw["download"]["connect_timeout_s"]),
            read_timeout=float(self.cfg.raw["download"]["read_timeout_s"]),
        )
        wb = self.cfg.raw["wayback"]
        self.wayback = WaybackFetcher(
            self.client, fetch_base_url=wb["fetch_base_url"],
            requests_per_sec=float(wb["requests_per_sec_per_worker"]),
            retry_after_429_s=tuple(wb["retry_after_429_s"]),
        )
        self.robots = RobotsCache(
            self.reg,
            fetcher=lambda url: fetch_text(self.client, url),
            ttl_hours=int(self.cfg.raw["politeness"]["robots_cache_ttl_hours"]),
        )

        self.writer.start()
        grace = float(self.cfg.raw["download"].get("shutdown_grace_s", 30))
        workers = asyncio.gather(
            *(self._worker(i, shard) for i, shard in enumerate(shards)),
            return_exceptions=True,
        )
        stop_waiter = asyncio.create_task(self.stop_event.wait())
        try:
            done, _ = await asyncio.wait({asyncio.ensure_future(workers), stop_waiter},
                                         return_when=asyncio.FIRST_COMPLETED)
            if not workers.done():
                # SIGTERM/SIGINT: let in-flight files finish within the
                # grace window, then cancel (long backoff sleeps must not
                # block shutdown; unclaimed rows recover at next start).
                try:
                    await asyncio.wait_for(workers, timeout=grace)
                except asyncio.TimeoutError:
                    log.warning("shutdown grace of %.0fs exceeded; cancelling workers", grace)
                    workers.cancel()
                    try:
                        await workers
                    except asyncio.CancelledError:
                        pass
        finally:
            stop_waiter.cancel()
            await self.writer.stop()
            await self.client.aclose()
        log.info("download stage done: %s", self.stats)
        return self.stats

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:  # pragma: no cover (non-unix)
                pass

    def _part_path(self, url_id: int) -> Path:
        # pid suffix: unique even if a second downloader process races
        # the same URL (e.g. after a crash-requeue).
        return self.tmp_dir / f"{url_id}.{os.getpid()}.part"

    def _cleanup_stale_parts(self, max_age_s: float = 3600) -> None:
        import time
        cutoff = time.time() - max_age_s
        for part in self.tmp_dir.glob("*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
            except OSError:
                pass

    def _recover_stale_claims(self) -> None:
        """Rows stuck in 'downloading' are from a crashed run: reclaim."""
        stale = self.reg.conn.execute(
            "SELECT COUNT(*) FROM urls WHERE status='downloading'"
        ).fetchone()[0]
        if stale:
            log.warning("recovering %d URLs stuck in 'downloading' from a previous crash", stale)
            with self.reg.tx():
                self.reg.conn.execute(
                    "UPDATE urls SET status='discovered', updated_at=? WHERE status='downloading'",
                    (utcnow(),),
                )

    def _shard_domains(self, domains: list[str]) -> list[list[str]]:
        n = min(self.concurrency, len(domains))
        shards: list[list[str]] = [[] for _ in range(n)]
        for domain in sorted(domains):
            shards[self._stable_shard(domain, n)].append(domain)
        return [s for s in shards if s]

    @staticmethod
    def _stable_shard(domain: str, n: int) -> int:
        import hashlib
        return int.from_bytes(hashlib.sha1(domain.encode()).digest()[:4], "big") % n

    # ------------------------------------------------------------------
    async def _worker(self, worker_id: int, domains: list[str]) -> None:
        log.debug("worker %d starting with %d domains", worker_id, len(domains))
        active = {d: True for d in domains}
        while not self.stop_event.is_set() and any(active.values()):
            for domain in domains:
                if self.stop_event.is_set():
                    break
                if not active[domain]:
                    continue
                rows = await self.writer.call(
                    self.reg.claim_urls, [domain], self.claim_batch_size
                )
                if not rows:
                    active[domain] = False
                    continue
                for row in rows:
                    if self.stop_event.is_set():
                        # un-claim gracefully; finish nothing new
                        self.writer.update_url(row["id"], status="discovered")
                        continue
                    try:
                        await self._process_url(dict(row))
                    except Exception:
                        log.exception("unexpected error processing %s", row["url"])
                        self.writer.update_url(row["id"], status="discovered")
            # domains exhausted this round; small breather before rescan
            if any(active.values()):
                await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    async def _process_url(self, row: dict) -> None:
        url, url_id, domain = row["url"], row["id"], row["domain"]

        while not has_free_space(self.tmp_dir, self.min_free_gb):
            log.warning("free disk below %.0fGB; download paused 60s", self.min_free_gb)
            await self.writer.call(self.reg.log_event, "disk_pause", None, None)
            if self.stop_event.is_set():
                self.writer.update_url(url_id, status="discovered")
                return
            await asyncio.sleep(60)

        if row["attempt_count"] > self.max_attempts:
            await self._wayback_or_dead(row, reason="max attempts exceeded")
            return

        status = self.breaker.domain_status(domain)
        if status in ("parked", "blacklisted"):
            await self._wayback_or_dead(row, reason=f"domain {status}")
            return
        if status == "backoff":
            await asyncio.sleep(min(self.breaker.backoff_remaining(domain), 5))
            self.writer.update_url(url_id, status="discovered")
            self.stats["requeued"] += 1
            return

        verdict = await self.robots.verdict(url)
        self.limiter.set_crawl_delay(domain, verdict.crawl_delay)
        if not verdict.allowed:
            self.writer.update_url(url_id, status="rejected", robots_status=verdict.status,
                                   reject_reason="robots_disallowed")
            self.stats["rejected"] += 1
            return

        if self.dry_run:
            log.info("[dry-run] would download %s", url)
            self.writer.update_url(url_id, status="discovered", robots_status=verdict.status)
            return

        # HEAD gate
        if self.head_before_get:
            await self.limiter.wait(domain)
            head_result = await self._head_gate(url)
            if head_result == "skip_html":
                # Origin now serves an HTML page instead of the file
                # (moved/deleted): try the archived copy.
                await self._wayback_or_dead(row, reason="origin_serves_html",
                                            robots=verdict.status)
                return
            if head_result == "skip_size":
                self.writer.update_url(url_id, status="rejected", robots_status=verdict.status,
                                       reject_reason="head_gate_size")
                self.stats["rejected"] += 1
                return
            if head_result == "retry":
                await self._handle_retryable(row, None, verdict.status)
                return

        # GET
        await self.limiter.wait(domain)
        part_path = self._part_path(url_id)
        try:
            result = await self._stream_download(url, part_path)
        except httpx.HTTPError as exc:
            log.info("network error on %s: %s", url, exc)
            self.breaker.record_failure(domain, None)
            await self._handle_retryable(row, None, verdict.status)
            part_path.unlink(missing_ok=True)
            return

        http_status, sha256, size, oversize = result
        if http_status in RETRYABLE_STATUS:
            part_path.unlink(missing_ok=True)
            self.breaker.record_failure(domain, http_status)
            await self._handle_retryable(row, http_status, verdict.status)
            return
        self.breaker.record_success(domain)

        if http_status in (404, 410) or http_status >= 400:
            part_path.unlink(missing_ok=True)
            await self._wayback_or_dead(row, reason=f"http {http_status}",
                                        origin_status=http_status, robots=verdict.status)
            return
        if oversize:
            part_path.unlink(missing_ok=True)
            self.writer.update_url(url_id, status="rejected", http_status=http_status,
                                   robots_status=verdict.status, reject_reason="oversize_body")
            self.stats["rejected"] += 1
            return
        if size < self.min_bytes:
            part_path.unlink(missing_ok=True)
            self.writer.update_url(url_id, status="rejected", http_status=http_status,
                                   robots_status=verdict.status, reject_reason="undersize_body")
            self.stats["rejected"] += 1
            return

        await self._finalize_payload(row, part_path, sha256, size, http_status,
                                     verdict.status, retrieval="origin")

    # ------------------------------------------------------------------
    async def _head_gate(self, url: str) -> str:
        """'ok' | 'skip_html' | 'skip_size' | 'retry'. HEAD failures are
        never fatal (many servers reject HEAD) -- only a *successful*
        HEAD may gate."""
        try:
            resp = await self.client.head(url)
        except httpx.HTTPError:
            return "ok"
        if resp.status_code in RETRYABLE_STATUS:
            return "retry"
        if resp.status_code >= 400:  # let GET see the definitive 404
            return "ok"
        ctype = resp.headers.get("content-type", "").lower().split(";")[0].strip()
        if ctype and ctype.startswith("text/html"):
            return "skip_html"
        if ctype and not any(ctype.startswith(t) for t in _DOC_CONTENT_TYPES):
            log.debug("suspicious content-type %s for %s; letting GET+magic decide", ctype, url)
        clen = resp.headers.get("content-length")
        if clen and clen.isdigit():
            n = int(clen)
            if n > self.max_bytes or n < self.min_bytes:
                return "skip_size"
        return "ok"

    async def _stream_download(self, url: str, part_path: Path
                               ) -> tuple[int, str, int, bool]:
        """Stream body to part_path. Returns (status, sha256, size, oversize)."""
        hasher = StreamingSha256()
        size = 0
        oversize = False
        async with self.client.stream("GET", url) as resp:
            if resp.status_code != 200:
                return resp.status_code, "", 0, False
            with open(part_path, "wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=1 << 16):
                    size += len(chunk)
                    if size > self.max_bytes:
                        oversize = True
                        break
                    hasher.update(chunk)
                    fh.write(chunk)
        return 200, hasher.hexdigest(), size, oversize

    async def _handle_retryable(self, row: dict, http_status: int | None,
                                robots_status: str) -> None:
        """Requeue after a retryable failure; breaker already updated."""
        domain_state = self.breaker.domain_status(row["domain"])
        if domain_state in ("parked", "blacklisted"):
            await self._wayback_or_dead(row, reason=f"domain {domain_state}",
                                        origin_status=http_status, robots=robots_status)
            return
        self.writer.update_url(row["id"], status="discovered",
                               http_status=http_status, robots_status=robots_status)
        self.stats["requeued"] += 1

    # ------------------------------------------------------------------
    async def _wayback_or_dead(self, row: dict, reason: str,
                               origin_status: int | None = None,
                               robots: str | None = None) -> None:
        url_id, url = row["id"], row["url"]
        if self.wayback_enabled and not self.dry_run:
            # Snapshot timestamp recorded at discovery (wayback_cdx
            # harvester) lets us fetch the archive directly.
            known_ts = None
            meta_raw = row.get("metadata")
            if isinstance(meta_raw, str) and meta_raw:
                import json
                try:
                    known_ts = json.loads(meta_raw).get("wayback_timestamp")
                except ValueError:
                    pass
            payload, snapshot_url, wb_status = await self.wayback.fetch(url, timestamp=known_ts)
            if payload:
                part_path = self._part_path(url_id)
                hasher = StreamingSha256()
                hasher.update(payload)
                part_path.write_bytes(payload)
                self.stats["wayback"] += 1
                await self._finalize_payload(
                    row, part_path, hasher.hexdigest(), len(payload),
                    origin_status or wb_status or 0, robots or "unavailable",
                    retrieval="wayback", snapshot_url=snapshot_url,
                )
                return
        self.writer.update_url(url_id, status="dead", http_status=origin_status,
                               robots_status=robots, reject_reason=reason)
        self.stats["dead"] += 1

    # ------------------------------------------------------------------
    async def _finalize_payload(self, row: dict, part_path: Path, sha256: str,
                                size: int, http_status: int, robots_status: str,
                                retrieval: str, snapshot_url: str | None = None) -> None:
        url_id, url = row["id"], row["url"]

        # Magic sniff: kills HTML error pages served as 200 immediately,
        # and enforces allowed_formats by CONTENT (a url ending .pptx can
        # still serve a PDF -- reject it here, before it costs disk).
        magic = sniff_format(part_path)
        fmt = {"zip": "pptx", "ole2": "ppt", "pdf": "pdf"}.get(magic or "", None)
        allowed = set(self.cfg.raw.get("allowed_formats", ["pptx", "ppt"]))
        if fmt is None or fmt not in allowed:
            part_path.unlink(missing_ok=True)
            if fmt is None and retrieval == "origin":
                # 200 + non-document body (HTML "moved" page etc.):
                # the link is effectively dead -> try the archived copy.
                await self._wayback_or_dead(row, reason="bad_magic_bytes",
                                            origin_status=http_status,
                                            robots=robots_status)
                return
            reason = "bad_magic_bytes" if fmt is None else f"format_not_allowed:{fmt}"
            self.writer.update_url(url_id, status="rejected", http_status=http_status,
                                   robots_status=robots_status, reject_reason=reason)
            self.stats["rejected"] += 1
            return

        origin_of_hash = await self.writer.call(self.reg.known_hash_origin, sha256)
        if origin_of_hash == "pipeline":
            # Hash seen by this pipeline before: only a duplicate if some
            # OTHER url or a files row owns it -- otherwise this is the
            # same URL re-downloaded after a crash lost its status update.
            is_dup = (await self.writer.call(self.reg.other_url_with_hash, sha256, url_id)
                      or await self.writer.call(self.reg.file_by_sha256, sha256) is not None)
        elif origin_of_hash is not None:
            is_dup = True    # catalog import / peer node: always a duplicate
        else:
            is_dup = await self.writer.call(self.reg.file_by_sha256, sha256) is not None
        if is_dup:
            part_path.unlink(missing_ok=True)
            self.writer.update_url(url_id, status="duplicate", sha256=sha256,
                                   http_status=http_status, robots_status=robots_status,
                                   retrieval_method=retrieval, content_length=size)
            self.stats["duplicates"] += 1
            return

        final_path = self.tmp_dir / f"{sha256}{Path(urlsplit(url).path).suffix.lower() or '.bin'}"
        os.replace(part_path, final_path)

        metadata = dict()
        if snapshot_url:
            metadata["wayback_snapshot_url"] = snapshot_url
        metadata["original_filename"] = _filename_from_url(url)
        metadata["local_path"] = str(final_path)
        metadata["download_ts"] = utcnow()
        existing_meta = row.get("metadata")
        if isinstance(existing_meta, str) and existing_meta not in ("", "{}"):
            import json
            try:
                metadata = {**json.loads(existing_meta), **metadata}
            except ValueError:
                pass

        self.writer.update_url(
            url_id, status="downloaded", sha256=sha256, http_status=http_status,
            robots_status=robots_status, retrieval_method=retrieval,
            content_length=size, metadata=metadata,
        )
        await self.writer.call(self.reg.add_known_hashes, [sha256], "pipeline")
        self.stats["downloaded"] += 1
        log.info("downloaded %s (%d bytes, %s, %s)", url, size, sha256[:12], retrieval)
