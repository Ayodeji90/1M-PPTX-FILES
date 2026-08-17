"""Extract stage: turn classified decks/PDFs into per-page PNG deliverables.

For each classified DELIVER file (payload on disk, feature vectors
present, pages not yet extracted):
  1. select_graphical_pages(feature_vectors)  -- pure, from stored vectors
  2. render selected pages -> PNG (soffice -> pdf -> pdftoppm)
  3. dedup: exact sha256 + perceptual near-dup (phash) against known pages
  4. write `pages` rows: status='extracted' (or 'duplicate')

Idempotent and resumable: pages rows are keyed (file_id, page_index);
a crash re-processes only files with no extracted pages yet. Rendering
is CPU-heavy (soffice + pdftoppm), so it runs in a bounded thread pool
(extract.max_concurrent) and obeys the same disk floor as other stages.
"""
from __future__ import annotations

import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..config import Config
from ..db.dao import Registry
from ..extract.render import render_file_pages
from ..extract.select import select_graphical_pages
from ..utils.disk import free_gb
from ..utils.hashing import sha256_file
from ..utils.perceptual import dhash

log = logging.getLogger("pptxsweeper.extract")


class ExtractStage:
    def __init__(self, cfg: Config, reg: Registry, dry_run: bool = False):
        self.cfg = cfg
        self.reg = reg
        self.dry_run = dry_run
        ext = cfg.raw.get("extract", {})
        self.dpi = int(ext.get("dpi", 150))
        self.max_concurrent = max(1, int(ext.get("max_concurrent", 2)))
        self.page_timeout_s = int(ext.get("page_timeout_s", 120))
        self.conv_timeout_s = int(ext.get("conv_timeout_s", 180))
        self.phash_distance = int(ext.get("phash_distance", 10))
        self.soffice_bin = cfg.raw.get("conversion", {}).get("soffice_bin", "soffice")
        self.pdftoppm_bin = ext.get("pdftoppm_bin", "pdftoppm")
        self.pages_dir = cfg.path("paths", "pages_dir")
        self.tmp_dir = cfg.path("paths", "download_tmp_dir")
        self.chunk = int(cfg.raw.get("extract", {}).get("chunk_limit", 50))
        self.min_free_gb = float(cfg.raw.get("extract", {}).get("min_free_disk_gb", 2))
        self.stats = {"extracted": 0, "duplicate": 0, "rejected": 0, "errors": 0}

    # ------------------------------------------------------------------
    def run(self, limit: int | None = None) -> dict:
        """Extract pages from classified DELIVER files that lack pages."""
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        if limit is None:
            limit = self.chunk
        rows = self.reg.conn.execute(
            """SELECT f.*, u.url AS source_url, u.domain AS source_domain,
                      u.created_at AS collection_ts, u.http_status,
                      u.robots_status, u.retrieval_method,
                      u.metadata AS url_metadata
               FROM files f JOIN urls u ON u.id = f.url_id
               WHERE f.decision='DELIVER' AND f.delivered_at IS NULL
                 AND f.local_path IS NOT NULL
                 AND f.feature_vectors IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM pages p WHERE p.file_id = f.id)
               ORDER BY f.id LIMIT ?""",
            (limit,),
        ).fetchall()
        log.info("extract: %d classified files pending page extraction%s",
                 len(rows), " [DRY RUN]" if self.dry_run else "")
        if not rows:
            return self.stats

        # Bounded rendering pool: soffice + pdftoppm are CPU-heavy and each
        # deck->pdf conversion can spike RAM; 2 concurrent renders is right
        # for a 4-vCPU box that also runs classify.
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            futures = {}
            for r in rows:
                if free_gb(self.pages_dir) < self.min_free_gb:
                    log.warning("extract paused: free disk %.1fGB < %.1fGB floor",
                                free_gb(self.pages_dir), self.min_free_gb)
                    break
                if self.dry_run:
                    log.info("[dry-run] would extract pages from %s",
                             Path(r["local_path"]).name)
                    continue
                futures[pool.submit(self._extract_one, dict(r))] = r
            for fut in as_completed(futures):
                try:
                    stat = fut.result()
                except Exception:
                    log.exception("extract worker failed")
                    self.stats["errors"] += 1
                    continue
                for k in ("extracted", "duplicate", "rejected"):
                    self.stats[k] += stat.get(k, 0)
        log.info("extract done: %s", self.stats)
        return self.stats

    # ------------------------------------------------------------------
    def _extract_one(self, row: dict) -> dict:
        """Select + render + dedup pages for one file. Returns per-kind counts."""
        file_id = row["id"]
        payload = Path(row["local_path"])
        if not payload.exists():
            self.reg.update_file(file_id, local_path=None)
            log.warning("payload missing for file %s; cleared local_path", file_id)
            return {"extracted": 0, "duplicate": 0, "rejected": 0}

        try:
            vectors = json.loads(row["feature_vectors"] or "[]")
        except ValueError:
            vectors = []
        selection = select_graphical_pages(vectors)
        if not selection:
            # No graphical pages: the deck contributes nothing to image
            # delivery. Mark the file so we never re-select it (pages row
            # with status 'rejected' acts as the tombstone).
            self.reg.insert_page(file_id=file_id, page_index=-1, status="rejected")
            return {"extracted": 0, "duplicate": 0, "rejected": 1}

        indexes = [s["index"] for s in selection]
        work = self.tmp_dir / f"extract_{file_id}"
        res = render_file_pages(
            payload, indexes, work, dpi=self.dpi,
            soffice_bin=self.soffice_bin, pdftoppm_bin=self.pdftoppm_bin,
            conv_timeout_s=self.conv_timeout_s, page_timeout_s=self.page_timeout_s)
        if not res.ok:
            shutil.rmtree(work, ignore_errors=True)
            self._record_pages_rejected(file_id, indexes, f"render:{res.reason}")
            return {"extracted": 0, "duplicate": 0, "rejected": len(indexes)}

        extracted = duplicate = rejected = 0
        try:
            for idx in indexes:
                png = res.pages.get(idx)
                if png is None or not png.exists():
                    rejected += 1
                    continue
                sha = sha256_file(png)
                phash = dhash(png.read_bytes())
                # Exact dup: same bytes already extracted/delivered anywhere
                # (a deck reusing the identical chart image on two slides IS
                # a duplicate deliverable).
                if self.reg.page_sha_known(sha):
                    png.unlink(missing_ok=True)
                    self.reg.insert_page(file_id=file_id, page_index=idx,
                                         sha256=sha, phash=phash, status="duplicate")
                    duplicate += 1
                    continue
                # Near-dup: perceptually same as an existing page from a
                # DIFFERENT source file. Pages of THIS file are never
                # compared -- several similar-styled chart pages of one deck
                # are all distinct deliverables.
                if self.reg.page_phash_known(phash, self.phash_distance,
                                             exclude_file_id=file_id):
                    png.unlink(missing_ok=True)
                    self.reg.insert_page(file_id=file_id, page_index=idx,
                                         sha256=sha, phash=phash, status="duplicate")
                    duplicate += 1
                    continue
                dest = self.pages_dir / f"{file_id}_{idx:03d}.png"
                shutil.move(str(png), dest)
                self.reg.insert_page(file_id=file_id, page_index=idx,
                                     sha256=sha, phash=phash, local_path=str(dest),
                                     status="extracted")
                extracted += 1
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return {"extracted": extracted, "duplicate": duplicate, "rejected": rejected}

    # ------------------------------------------------------------------
    def _record_pages_rejected(self, file_id: int, indexes: list[int], reason: str) -> None:
        for idx in indexes:
            self.reg.insert_page(file_id=file_id, page_index=idx, status="rejected")
        log.warning("file %s: %d page(s) rejected (%s)", file_id, len(indexes), reason)
