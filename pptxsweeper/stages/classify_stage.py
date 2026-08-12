"""Classify stage: full validation + .ppt conversion + quality engine +
compliance screens.

Per URL row in 'downloaded':
  validate -> (convert if .ppt) -> quality classify -> screens
  DELIVER -> payload moves to staging/, urls.status='classified'
  REVIEW  -> payload moves to review/ (size-capped; synced to Drive _review/)
  REJECT  -> payload deleted immediately, record kept

Every outcome inserts a `files` row with feature vectors + quality
report + compliance JSON, so re-classification and audits never need
the payload again. Idempotent and resumable: rows are processed one at
a time and committed in small batches; a crash re-processes at most the
in-flight file (dedup on files.sha256 handles the replay).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from concurrent.futures import (FIRST_COMPLETED, ProcessPoolExecutor,
                                ThreadPoolExecutor, wait)
from pathlib import Path

from ..compliance.screens import run_screens
from ..config import Config
from ..db.dao import Registry
from ..convert import convert_ppt_to_pptx
from ..download.validate import validate_payload
from ..quality import classify as quality_classify

log = logging.getLogger("pptxsweeper.classify")


def _ram_available_mb() -> int | None:
    """Available RAM in MiB via /proc/meminfo (Linux); None elsewhere.
    Used to cap the worker-process pool so a small VM never OOMs."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        return None
    return None


class _FileTimeout(Exception):
    """Raised by the SIGALRM handler so a per-file compute timeout is
    distinguishable from a TimeoutError raised elsewhere (e.g. a legacy
    .ppt conversion timing out) -- both reject the file, but the reason
    recorded stays accurate."""


def _compute_task(task: dict) -> dict:
    """Pure per-file compute (validation, quality engine, screens) -- runs
    in a worker PROCESS; no registry access, plain dicts in and out so it
    pickles cleanly. Legacy .ppt conversion runs in the PARENT process
    (bounded by conversion.max_concurrent) so worker processes never spawn
    LibreOffice; `preconverted` tasks are validated as converted .pptx.

    A pathological payload must never wedge a worker (and thus the whole
    deliver chain) forever: `file_timeout_s` arms a SIGALRM so a hung
    validate/quality pass is turned into a reject instead. Runs in the
    worker's main thread, where signal.alarm is legal."""
    import signal as _signal
    from ..download.validate import validate_payload
    from ..quality import classify as quality_classify
    from ..compliance.screens import run_screens

    payload = Path(task["payload"])
    preconverted = bool(task.get("preconverted"))
    timeout_s = int(task.get("file_timeout_s") or 0)

    def _run() -> dict:
        run_payload = Path(task["payload"])
        out: dict = {"url_id": task["url_id"], "payload": str(run_payload),
                     "converted": 1 if preconverted else 0,
                     "reject": None, "format": None, "slide_count": 0}
        v = validate_payload(run_payload)
        if not v.ok:
            prefix = "converted_pptx_invalid" if preconverted else "validation"
            out["reject"] = f"{prefix}:{v.reason}"
            return out
        if v.format not in task["allowed_formats"]:
            out["reject"] = f"format_not_allowed:{v.format}"
            return out
        # Native .ppt normally never reaches here (converted in the parent);
        # keep a worker-side fallback for direct API use.
        if v.format == "ppt":
            from ..convert import convert_ppt_to_pptx
            res = convert_ppt_to_pptx(run_payload, Path(task["tmp_dir"]),
                                      soffice_bin=task["soffice_bin"],
                                      timeout_s=task["conv_timeout"])
            if not res.ok:
                out["reject"] = f"ppt_conversion_failed:{res.reason}"
                return out
            run_payload.unlink(missing_ok=True)
            run_payload = res.output_path
            out["payload"] = str(run_payload)
            out["converted"] = 1
            v = validate_payload(run_payload)
            if not v.ok:
                out["reject"] = f"converted_pptx_invalid:{v.reason}"
                return out
        out["format"] = v.format
        report = quality_classify(run_payload, thresholds=task["thresholds"],
                                  image_thresholds=task["image_thresholds"],
                                  ocr_ambiguous_only=task["ocr"])
        qrd = report.to_dict()   # carries doc_properties (extracted in one zip open)
        screens = run_screens(report.full_text, robots_status=task["robots_status"])
        out.update(
            quality=report.quality, decision=report.decision,
            feature_vectors=report.feature_vectors_json(),
            quality_report=qrd, compliance=screens.to_dict(),
            forces_reject=screens.forces_reject, forces_review=screens.forces_review,
            screen_details=str(screens.details)[:300],
            slide_count=report.slide_count or v.slide_count,
            explanation=(report.explanations[-1] if report.explanations else "")[:300],
        )
        return out

    def _timeout_handler(_signum, _frame):
        raise _FileTimeout(f"classify file timeout after {timeout_s}s")

    if timeout_s > 0:
        old_handler = _signal.signal(_signal.SIGALRM, _timeout_handler)
        _signal.alarm(timeout_s)
    try:
        return _run()
    except _FileTimeout:
        # Pathological payload: reject it (payload deleted by caller) so it
        # can never hang a worker again -- the row ends terminal.
        return {"url_id": task["url_id"], "payload": str(payload),
                "converted": 1 if preconverted else 0,
                "reject": "classify_timeout", "format": None, "slide_count": 0}
    finally:
        if timeout_s > 0:
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, old_handler)


class ClassifyStage:
    def __init__(self, cfg: Config, reg: Registry, dry_run: bool = False):
        self.cfg = cfg
        self.reg = reg
        self.dry_run = dry_run
        self.staging_dir = cfg.path("paths", "staging_dir")
        self.review_dir = cfg.path("paths", "review_dir")
        self.tmp_dir = cfg.path("paths", "download_tmp_dir")
        self.thresholds = dict(cfg.raw["quality"])
        self.image_thresholds = dict(cfg.raw["quality"]["image_signals"])
        # Pipeline's OWN OCR (pytesseract) tie-breaker for ambiguous images.
        # It's a big CPU sink (each ambiguous image spawns tesseract), so it
        # can be turned off entirely with quality.use_ocr=false -- the engine
        # degrades gracefully to the other image signals. This is pptxsweeper's
        # OCR only; it has nothing to do with any external OCR service.
        use_ocr = bool(cfg.raw["quality"].get("use_ocr", True))
        self.ocr_ambiguous_only = use_ocr and bool(cfg.raw["quality"]["ocr_ambiguous_only"])
        conv = cfg.raw["conversion"]
        self.soffice_bin = conv["soffice_bin"]
        self.conversion_timeout = int(conv["timeout_s"])
        self.stats = {"deliver": 0, "review": 0, "reject": 0, "errors": 0}
        self.file_timeout_s = int(self.cfg.raw.get("classify", {})
                                  .get("file_timeout_s", 0))
        self.max_run_minutes = float(self.cfg.raw.get("classify", {})
                                     .get("max_run_minutes", 0))

    # ------------------------------------------------------------------
    def run(self, limit: int | None = None) -> dict:
        """Parallel classification: the pure compute (validate/convert/
        quality/screens) fans out to one worker process per CPU core;
        this process does all registry writes and file moves."""
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)
        if limit is None:
            limit = int(self.cfg.raw.get("classify", {}).get("chunk_limit", 150)) or None
        rows = self.reg.urls_by_status("downloaded", limit=limit)
        log.info("classifying %d downloaded files%s", len(rows),
                 " [DRY RUN]" if self.dry_run else "")
        if not rows:
            return self.stats

        self._done = threading.Event()
        if self.max_run_minutes:
            threading.Thread(target=self._watchdog, daemon=True).start()
        try:
            return self._run_workers(rows)
        finally:
            self._done.set()

    # ------------------------------------------------------------------
    def _run_workers(self, rows) -> dict:
        """Body of run() after the empty check; wrapped so the watchdog
        can observe completion. Keeps run() readable."""
        pairs: list[tuple[dict, dict]] = []   # (row, task)
        allowed = tuple(self.cfg.raw.get("allowed_formats", ["pptx", "ppt"]))
        seen_shas: set[str] = set()
        for r in rows:
            row = dict(r)
            # Same content downloaded via two URLs (parallel-download race
            # or prior run): only the first row owns the payload.
            sha = row.get("sha256")
            if sha and (sha in seen_shas or self.reg.file_by_sha256(sha)):
                self.reg.update_url(row["id"], status="duplicate")
                continue
            if sha:
                seen_shas.add(sha)
            payload = self._payload_path(row)
            if payload is None:
                log.warning("payload missing for url %s; requeueing", row["url"])
                self.reg.update_url(row["id"], status="discovered")
                continue
            if self.dry_run:
                log.info("[dry-run] would classify %s", payload.name)
                continue
            pairs.append((row, {
                "url_id": row["id"], "payload": str(payload),
                "allowed_formats": allowed, "tmp_dir": str(self.tmp_dir),
                "soffice_bin": self.soffice_bin, "conv_timeout": self.conversion_timeout,
                "thresholds": self.thresholds, "image_thresholds": self.image_thresholds,
                "ocr": self.ocr_ambiguous_only, "robots_status": row.get("robots_status"),
                "file_timeout_s": self.file_timeout_s,
            }))
        if not pairs:
            return self.stats

        # RAM-aware worker cap: each worker holds numpy/PIL/lxml + one
        # deck's images; never spawn more workers than available RAM allows
        # (config classify.worker_memory_mb sets the per-worker budget).
        workers = int(self.cfg.raw.get("classify", {}).get("workers", 0)) or os.cpu_count() or 2
        mem_budget_mb = int(self.cfg.raw.get("classify", {}).get("worker_memory_mb", 512))
        avail_mb = _ram_available_mb()
        if avail_mb is not None:
            workers = min(workers, max(1, avail_mb // mem_budget_mb))
        workers = min(workers, len(pairs))

        # Legacy .ppt files convert in THIS process through one bounded
        # conversion queue (conversion.max_concurrent): LibreOffice RAM
        # usage stays predictable on small VMs and worker processes never
        # spawn soffice. Conversions OVERLAP with quality-classification:
        # native pptx tasks go to the worker pool immediately, and each
        # converted file joins the pool the moment its conversion finishes.
        from ..download.validate import sniff_format
        conv_max = max(1, int(self.cfg.raw.get("conversion", {})
                              .get("max_concurrent", 2)))
        conv_pairs, native_pairs = [], []
        for pair in pairs:
            payload = Path(pair[1]["payload"])
            if payload.suffix.lower() == ".ppt" or sniff_format(payload) == "ole2":
                conv_pairs.append(pair)
            else:
                native_pairs.append(pair)

        if workers <= 1:
            # single process: bounded conversions first, then sequential
            # classify (converted results are plain dicts, no pool needed)
            converted: list[tuple[dict, dict]] = []
            if conv_pairs:
                with ThreadPoolExecutor(max_workers=conv_max) as cpool:
                    for (row, task), result in zip(
                            conv_pairs,
                            cpool.map(self._parent_convert, (t for _, t in conv_pairs))):
                        if result.get("reject"):
                            self._persist_safe(row, result)
                        else:
                            converted.append((row, result))
            ordered = native_pairs + converted
            for (row, _), result in zip(ordered,
                                        map(_compute_task, (t for _, t in ordered))):
                self._persist_safe(row, result)
        else:
            with ProcessPoolExecutor(max_workers=workers) as pool, \
                    ThreadPoolExecutor(max_workers=conv_max) as cpool:
                pending: dict = {}        # classify future -> row
                conversions: dict = {}    # convert future -> (row, task)
                for row, task in native_pairs:
                    pending[pool.submit(_compute_task, task)] = row
                for row, task in conv_pairs:
                    conversions[cpool.submit(self._parent_convert, task)] = (row, task)
                while pending or conversions:
                    done, _ = wait([*pending.keys(), *conversions.keys()],
                                   return_when=FIRST_COMPLETED)
                    for fut in done:
                        if fut in conversions:
                            row, _task = conversions.pop(fut)
                            result = fut.result()
                            if result.get("reject"):
                                self._persist_safe(row, result)
                            else:
                                # converted file joins the pool right away
                                pending[pool.submit(_compute_task, result)] = row
                        else:
                            row = pending.pop(fut)
                            self._persist_safe(row, fut.result())
        log.info("classify done (%d workers): %s", workers, self.stats)
        return self.stats

    def _watchdog(self) -> None:
        """Last-resort whole-run watchdog: if a classify pass exceeds
        `max_run_minutes` (e.g. a worker hung past its per-file timeout,
        or the pool deadlocked), force-exit the process so the orchestrator
        re-runs the stage. Rows stay 'downloaded' and are re-picked.

        Note: SIGALRM fires at bytecode boundaries, so a hang INSIDE a C
        extension (lxml/PIL/numpy tight loop) can evade the per-file
        timeout; in that case the watchdog converts a permanent wedge into
        a slow restart loop. Still a strict improvement -- and os._exit
        briefly orphans the in-flight worker processes, which exit on
        their own once their current task finishes and the task pipe
        closes (SQLite is WAL crash-safe, rows are re-picked)."""
        if not self.max_run_minutes:
            return
        deadline = time.monotonic() + self.max_run_minutes * 60
        while time.monotonic() < deadline:
            time.sleep(30)
            if self._done.is_set():
                return
        log.error("classify run exceeded %.0f min watchdog; force-exiting",
                  self.max_run_minutes)
        os._exit(1)

    def _parent_convert(self, task: dict) -> dict:
        """Convert a legacy .ppt in this (parent) process. Runs inside the
        bounded conversion thread pool; the converted .pptx is handed back
        as a preconverted task (ALL original task fields preserved -- the
        worker needs thresholds/allowed_formats/etc.), or a reject dict on
        failure (the original .ppt payload is deleted by the caller's
        _persist)."""
        from ..convert import convert_ppt_to_pptx
        payload = Path(task["payload"])
        out = dict(task)   # keep every field the worker will need
        out.update({"converted": 1, "preconverted": 1, "reject": None,
                    "format": None, "slide_count": 0})
        try:
            res = convert_ppt_to_pptx(payload, Path(task["tmp_dir"]),
                                      soffice_bin=task["soffice_bin"],
                                      timeout_s=task["conv_timeout"])
        except Exception as exc:
            out["reject"] = f"ppt_conversion_failed:{exc}"
            return out
        if not res.ok:
            out["reject"] = f"ppt_conversion_failed:{res.reason}"
            return out
        payload.unlink(missing_ok=True)
        out["payload"] = str(res.output_path)
        return out

    def _persist_safe(self, row: dict, result: dict) -> None:
        try:
            self._persist(row, result)
        except FileNotFoundError:
            # payload vanished mid-flight (another row of the same content
            # already moved it): duplicate, not an error
            self.reg.update_url(row["id"], status="duplicate")
        except Exception:
            log.exception("classify persist failed for url id %s", row["id"])
            self.stats["errors"] += 1

    def _persist(self, row: dict, result: dict) -> None:
        """Registry writes + file moves for one computed result."""
        url_id = row["id"]
        payload = Path(result["payload"])
        meta = json.loads(row.get("metadata") or "{}")
        original_filename = meta.get("original_filename") or payload.name

        if result["reject"]:
            self._reject(row, payload, result["reject"], original_filename,
                         converted=result["converted"])
            return

        decision = result["decision"]
        quality_report = result["quality_report"]
        if result["forces_reject"]:
            decision = "REJECT"
            quality_report["explanations"].append(
                f"compliance reject: {result['screen_details']}")
        elif result["forces_review"] and decision == "DELIVER":
            decision = "REVIEW"
            quality_report["explanations"].append(
                f"compliance review: {result['screen_details']}")

        file_fields = dict(
            url_id=url_id, sha256=row["sha256"],
            original_filename=original_filename,
            format=result["format"], file_size=payload.stat().st_size,
            slide_count=result["slide_count"], quality=result["quality"],
            decision=decision, feature_vectors=result["feature_vectors"],
            quality_report=quality_report, compliance=result["compliance"],
            converted_from_ppt=result["converted"],
        )
        existing = self.reg.file_by_sha256(row["sha256"])
        if decision == "REJECT":
            payload.unlink(missing_ok=True)
            file_fields["local_path"] = None
            self._upsert_file(existing, file_fields)
            reason = (quality_report.get("explanations") or ["quality reject"])[-1]
            self.reg.update_url(url_id, status="rejected", reject_reason=reason[:300])
            self.stats["reject"] += 1
        elif decision == "REVIEW":
            dest = self.review_dir / f"{row['sha256']}.{result['format']}"
            if payload.resolve() != dest.resolve():
                shutil.move(str(payload), dest)
            file_fields["local_path"] = str(dest)
            self._upsert_file(existing, file_fields)
            self.reg.update_url(url_id, status="review")
            self.stats["review"] += 1
        else:  # DELIVER
            dest = self.staging_dir / f"{row['sha256']}.{result['format']}"
            if payload.resolve() != dest.resolve():
                shutil.move(str(payload), dest)
            file_fields["local_path"] = str(dest)
            self._upsert_file(existing, file_fields)
            self.reg.update_url(url_id, status="classified")
            self.stats["deliver"] += 1

    # ------------------------------------------------------------------
    def _payload_path(self, row: dict) -> Path | None:
        try:
            meta = json.loads(row.get("metadata") or "{}")
        except ValueError:
            meta = {}
        p = meta.get("local_path")
        if p and Path(p).exists():
            return Path(p)
        # fallback: reconstruct from sha256 in tmp dir
        if row.get("sha256"):
            for candidate in self.tmp_dir.glob(f"{row['sha256']}.*"):
                return candidate
        return None

    def _upsert_file(self, existing, fields: dict) -> int:
        if existing:
            file_id = existing["id"]
            self.reg.update_file(file_id, **{k: v for k, v in fields.items()
                                             if k not in ("url_id", "sha256")})
            return file_id
        return self.reg.insert_file(**fields)

    def _reject(self, row: dict, payload: Path, reason: str,
                original_filename: str, converted: int = 0) -> None:
        payload.unlink(missing_ok=True)
        existing = self.reg.file_by_sha256(row["sha256"]) if row.get("sha256") else None
        fields = dict(
            url_id=row["id"], sha256=row["sha256"],
            original_filename=original_filename, decision="REJECT",
            quality="LOW", converted_from_ppt=converted,
            quality_report={"error": reason, "explanations": [reason]},
        )
        self._upsert_file(existing, fields)
        self.reg.update_url(row["id"], status="rejected", reject_reason=reason[:300])
        self.stats["reject"] += 1

    # ------------------------------------------------------------------
    def _write_review_sidecars(self) -> int:
        """Write a {sha}.metadata.json next to each local review payload so
        the Drive _review/ folder carries full metadata (source URL,
        quality report, doc properties, compliance, raw crawl record)."""
        from ..packager.manifest import metadata_record
        rows = self.reg.conn.execute(
            """SELECT f.*, u.url AS source_url, u.domain AS source_domain,
                      u.created_at AS collection_ts, u.http_status,
                      u.robots_status, u.retrieval_method,
                      u.metadata AS url_metadata
               FROM files f JOIN urls u ON u.id = f.url_id
               WHERE f.decision='REVIEW' AND f.delivered_at IS NULL
                 AND u.status='review'"""
        ).fetchall()
        written = 0
        for r in rows:
            rd = dict(r)
            if not rd.get("sha256") or not rd.get("format"):
                continue
            record = metadata_record(rd)
            record["final_status"] = "review"
            sidecar = self.review_dir / f"{rd['sha256']}.metadata.json"
            sidecar.write_text(json.dumps(record, indent=2, default=str),
                               encoding="utf-8")
            written += 1
        return written

    def sync_review_to_drive(self, rclone) -> None:
        """Sync review/ to Drive _review/ and prune local payloads to cap."""
        review_folder = self.cfg.raw["rclone"]["review_folder"]
        self._write_review_sidecars()
        rclone.mkdir(review_folder)
        rclone.copy_dir(self.review_dir, review_folder)
        if not rclone.check(self.review_dir, review_folder,
                            method=self.cfg.raw["rclone"]["verify_method"]):
            log.warning("review sync verification failed; keeping local payloads")
            return
        cap_bytes = float(self.cfg.raw["classify"]["review_dir_cap_gb"]) * 1024 ** 3
        files = sorted(self.review_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        total = sum(f.stat().st_size for f in files if f.is_file())
        for f in files:
            if total <= cap_bytes:
                break
            if f.is_file():
                total -= f.stat().st_size
                f.unlink()
                log.info("pruned reviewed payload %s (synced to Drive)", f.name)


def run_reclassify(cfg: Config, reg: Registry) -> dict:
    """Re-run decisions from stored feature vectors after threshold
    changes -- no file access, no re-download (contractual requirement)."""
    from ..quality import reclassify_from_vectors
    thresholds = dict(cfg.raw["quality"])
    stats = {"rechecked": 0, "changed": 0}
    rows = reg.conn.execute(
        "SELECT id, feature_vectors, format, quality, decision FROM files "
        "WHERE feature_vectors IS NOT NULL AND delivered_at IS NULL"
    ).fetchall()
    for row in rows:
        vectors = json.loads(row["feature_vectors"])
        report = reclassify_from_vectors(vectors, thresholds, fmt=row["format"] or "")
        stats["rechecked"] += 1
        if report.quality != row["quality"] or report.decision != row["decision"]:
            stats["changed"] += 1
            new_report = report.to_dict()
            # a fresh decide() report has no docProps; carry the retained
            # metadata (and image-signal audit lines) from the stored record
            # so a threshold re-run never wipes the audit trail
            try:
                stored = json.loads(row["quality_report"]) if row["quality_report"] else {}
            except ValueError:
                stored = {}
            new_report["doc_properties"] = stored.get("doc_properties") or {}
            # keep only the image-audit line from the old report: a fresh
            # decide() can never reproduce it, and appending the stale
            # decision lines would leave contradictory verdict text
            new_report["explanations"] = [
                *new_report.get("explanations", []),
                *(e for e in (stored.get("explanations") or [])
                  if "raster images" in e),
            ]
            reg.update_file(row["id"], quality=report.quality, decision=report.decision,
                            quality_report=new_report)
            # keep urls.status in sync for un-delivered files
            url_status = {"DELIVER": "classified", "REVIEW": "review",
                          "REJECT": "rejected"}[report.decision]
            url_row = reg.conn.execute("SELECT url_id FROM files WHERE id=?",
                                       (row["id"],)).fetchone()
            if url_row and url_row["url_id"]:
                reg.update_url(url_row["url_id"], status=url_status)
    log.info("reclassify done: %s", stats)
    return stats
