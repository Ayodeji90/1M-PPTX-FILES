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


def _ram_available_gb() -> float | None:
    """Free RAM in GiB via /proc/meminfo (Linux); None elsewhere."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError):
        return None
    return None


def _should_pause(reasons: set[str], backlog_count: int, backlog_cap: int,
                  ram_free_gb: float | None, ram_min_gb: float,
                  ram_resume_high_water: float = 1.2) -> set[str]:
    """Update the set of download-pause reasons; empty means proceed.

    Two independent gates: the 'downloaded' backlog cap (disk-buffer
    bound) and free RAM (classify/soffice need headroom on small VMs --
    a memory spike with no swap is what wedged the OS, killing sshd).
    Both use hysteresis so a count/measurement at the boundary does not
    flap pause/resume every loop."""
    if backlog_cap:
        if backlog_count >= backlog_cap:
            reasons.add("backlog")
        elif backlog_count <= backlog_cap * 0.8:
            reasons.discard("backlog")
    if ram_min_gb:
        if ram_free_gb is None:
            reasons.discard("ram")
        elif ram_free_gb < ram_min_gb:
            reasons.add("ram")
        elif ram_free_gb >= ram_min_gb * ram_resume_high_water:
            reasons.discard("ram")
    return reasons

def _filename_from_url(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "unnamed"
    return name[:255]


def _head_verdict(status_code: int, content_type: str | None,
                  content_length: str | None, max_bytes: int,
                  min_bytes: int) -> str:
    """Map a HEAD response to a gate verdict: 'ok' | 'skip_html' |
    'skip_size' | 'retry'.

    HEAD failures are never fatal (many servers reject HEAD) -- only a
    *successful* HEAD may gate. Only 429/5xx warrant a retry: a HEAD 403
    usually means the server rejects HEAD for this resource (e.g. S3
    presigned URLs are GET-signed only, so figshare links always 403 on
    HEAD) while GET succeeds -- fall through and let GET decide.
    """
    if status_code == 429 or status_code >= 500:
        return "retry"
    if status_code >= 400:  # let GET see the definitive 404/403
        return "ok"
    ctype = (content_type or "").lower().split(";")[0].strip()
    if ctype and ctype.startswith("text/html"):
        return "skip_html"
    if content_length and content_length.isdigit():
        n = int(content_length)
        if n > max_bytes or n < min_bytes:
            return "skip_size"
    return "ok"


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
        # Backlog cap: stop claiming new URLs once this many are sitting in
        # 'downloaded' (payloads on disk waiting for classify). Keeps the
        # download stage from outrunning classify and filling the disk.
        self.max_backlog = int(dl.get("max_downloaded_backlog", 0))
        self.min_free_ram_gb = float(dl.get("min_free_ram_gb", 0))
        # How often the stage re-scans for newly discovered domains. A
        # long-running download must not starve domains harvested after it
        # started (see _domain_refresher).
        self.domain_refresh_s = float(dl.get("domain_refresh_s", 300))
        self._backlog_paused = asyncio.Event()
        self._pause_reasons: set[str] = set()
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

        # NOTE: discovery is already node-sharded (see NodeIdentity /
        # harvesters), so every domain in THIS node's local registry is
        # ours to download -- domain-list sources were sharded by domain,
        # global/aggregator sources (Zenodo, IA, Brave, ...) by page, and
        # each node keeps its own DB. Re-sharding by domain here would drop
        # the global-source URLs (whose single domain hashes to one node)
        # that this node legitimately discovered, so we do NOT filter.
        domains = self.reg.distinct_domains("discovered")
        if not domains:
            log.info("no domains with discovered URLs; nothing to do")
            return self.stats
        # Dynamic work queue: up to `concurrency` domains are in flight at
        # once, each handled by exactly one worker at a time (politeness
        # preserved), and a domain is re-enqueued to the BACK after each
        # batch so all domains round-robin fairly. This keeps every worker
        # busy on a DIFFERENT domain instead of one worker serially walking
        # a static shard of domains (whose per-domain delays never overlap).
        self._domain_queue: asyncio.Queue[str] = asyncio.Queue()
        self._known_domains: set[str] = set(domains)
        for domain in sorted(domains):
            self._domain_queue.put_nowait(domain)
        self._active_domains = 0
        # Spawn the full pool up front: the domain refresher keeps adding
        # freshly harvested domains, so the worker count must not be pinned
        # by how few domains existed at startup (idle workers just wait).
        n_workers = self.concurrency
        log.info("downloading from %d domains across %d workers (node %d/%d)%s",
                 len(domains), n_workers, self.node.node_id, self.node.node_count,
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
        backlog_task = asyncio.create_task(self._backlog_monitor())
        refresher_task = asyncio.create_task(self._domain_refresher())
        workers = asyncio.gather(
            *(self._worker(i) for i in range(n_workers)),
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
            backlog_task.cancel()
            refresher_task.cancel()
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

    # ------------------------------------------------------------------
    async def _domain_refresher(self) -> None:
        """Keep the live domain queue in sync with the registry.

        The queue is built once at startup; without this a single domain
        that never drains (e.g. a HEAD-403 retry loop) pins the stage
        open, the orchestrator never re-runs `download`, and every domain
        harvested afterwards sits unclaimed forever. Every refresh, scan
        for discovered domains and enqueue any not yet known."""
        while not self.stop_event.is_set():
            await asyncio.sleep(self.domain_refresh_s)
            try:
                fresh = await self.writer.call(self.reg.distinct_domains, "discovered")
            except Exception:
                log.exception("domain refresh scan failed")
                continue
            added = 0
            for domain in fresh:
                if domain not in self._known_domains:
                    self._known_domains.add(domain)
                    self._domain_queue.put_nowait(domain)
                    added += 1
            if added:
                log.info("domain refresher: enqueued %d newly discovered domain(s) "
                         "(%d total known)", added, len(self._known_domains))

    async def _backlog_monitor(self) -> None:
        """Pause claiming when too many payloads are waiting for classify
        (bounds tmp_downloads so the disk can never fill) OR when free RAM
        is low (classify + LibreOffice conversions need headroom on small
        VMs -- a memory spike with no swap is what wedged the whole OS
        before, making sshd itself unresponsive). Runs in the download
        process; classify (a separate process) drains 'downloaded' rows,
        so the count drops and downloads resume."""
        while not self.stop_event.is_set():
            if self.max_backlog:
                n = await self.writer.call(self.reg.count_by_status, "downloaded")
            else:
                n = 0
            self._pause_reasons = _should_pause(
                self._pause_reasons, n, self.max_backlog,
                _ram_available_gb(), self.min_free_ram_gb)
            if self._pause_reasons and not self._backlog_paused.is_set():
                log.warning("pausing downloads: %s (backlog=%d/%d, free RAM %.1fGB)",
                            sorted(self._pause_reasons), n, self.max_backlog,
                            _ram_available_gb() or 0.0)
                self._backlog_paused.set()
            elif not self._pause_reasons and self._backlog_paused.is_set():
                log.info("resuming downloads (backlog %d, free RAM %.1fGB)",
                         n, _ram_available_gb() or 0.0)
                self._backlog_paused.clear()
            await asyncio.sleep(15)

    async def _worker(self, worker_id: int) -> None:
        """Pull a domain off the shared queue, download one polite batch
        from it, then re-enqueue it if it still has URLs. A domain is only
        ever held by one worker at a time, so per-domain serialism (and
        thus politeness) is preserved while up to `concurrency` DIFFERENT
        domains download in parallel."""
        queue = self._domain_queue
        while not self.stop_event.is_set():
            if self._backlog_paused.is_set():
                await asyncio.sleep(2)   # classify is behind; hold off claiming
                continue
            try:
                domain = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No domain waiting. If nothing is in flight either, the
                # run is drained -> exit; otherwise loop and wait again.
                if queue.empty() and self._active_domains == 0:
                    log.debug("worker %d exiting: work queue drained", worker_id)
                    return
                continue
            self._active_domains += 1
            try:
                rows = await self.writer.call(
                    self.reg.claim_urls, [domain], self.claim_batch_size
                )
                if not rows:
                    # Domain exhausted: drop it (don't requeue), and forget
                    # it so the refresher re-adds it if it gains new URLs.
                    self._known_domains.discard(domain)
                    continue
                for row in rows:
                    if self.stop_event.is_set():
                        self.writer.update_url(row["id"], status="discovered")
                        continue
                    try:
                        await self._process_url(dict(row))
                    except Exception:
                        log.exception("unexpected error processing %s", row["url"])
                        self.writer.update_url(row["id"], status="discovered")
                if not self.stop_event.is_set():
                    queue.put_nowait(domain)   # more may remain: round-robin
            finally:
                self._active_domains -= 1
                queue.task_done()

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
        # Only 429/5xx on HEAD warrant a retry. A HEAD 403 is NOT
        # retryable: it usually means the server rejects HEAD for this
        # resource (e.g. S3 presigned URLs are GET-signed only, so figshare
        # links always 403 on HEAD) while GET succeeds -- fall through and
        # let GET decide.
        return _head_verdict(resp.status_code,
                             resp.headers.get("content-type"),
                             resp.headers.get("content-length"),
                             self.max_bytes, self.min_bytes)

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
            # STREAM to disk (never buffer a multi-hundred-MB archive in
            # RAM), enforcing the same min/max size gates as origin GETs.
            part_path = self._part_path(url_id)
            ok, sha256, size, snapshot_url, wb_status = await self.wayback.fetch_to_file(
                url, part_path, timestamp=known_ts,
                max_bytes=self.max_bytes, min_bytes=self.min_bytes)
            if ok:
                self.stats["wayback"] += 1
                await self._finalize_payload(
                    row, part_path, sha256, size,
                    origin_status or wb_status or 0, robots or "unavailable",
                    retrieval="wayback", snapshot_url=snapshot_url,
                )
                return
            part_path.unlink(missing_ok=True)
            if size >= self.max_bytes:
                self.writer.update_url(url_id, status="rejected", http_status=wb_status,
                                       robots_status=robots,
                                       reject_reason="wayback_oversize_body")
                self.stats["rejected"] += 1
                return
            if 0 < size < self.min_bytes:
                self.writer.update_url(url_id, status="rejected", http_status=wb_status,
                                       robots_status=robots,
                                       reject_reason="wayback_undersize_body")
                self.stats["rejected"] += 1
                return
            # no snapshot / fetch failed: terminal dead
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
            # OTHER url owns it or the existing files row is already
            # delivered -- otherwise this is the same URL re-downloaded
            # after a crash/lost-payload, and the re-download is how it
            # recovers (file_is_duplicate lets an undelivered same-url row
            # through so it can be re-delivered instead of stranded).
            is_dup = (await self.writer.call(self.reg.other_url_with_hash, sha256, url_id)
                      or await self.writer.call(self.reg.file_is_duplicate, sha256, url_id))
        elif origin_of_hash is not None:
            is_dup = True    # catalog import / peer node: always a duplicate
        else:
            is_dup = await self.writer.call(self.reg.file_is_duplicate, sha256, url_id)
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
