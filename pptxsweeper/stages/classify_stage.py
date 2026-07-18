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
import shutil
from pathlib import Path

from ..compliance.screens import run_screens
from ..config import Config
from ..db.dao import Registry
from ..convert import convert_ppt_to_pptx
from ..download.validate import validate_payload
from ..quality import classify as quality_classify

log = logging.getLogger("pptxsweeper.classify")

_CORE_PROP_TAGS = ("title", "creator", "subject", "description", "language",
                   "created", "modified", "keywords", "lastModifiedBy", "category")
_APP_PROP_TAGS = ("Company", "Application", "Slides", "Words", "PresentationFormat")


def extract_doc_properties(path: Path) -> dict:
    """OOXML document properties (docProps/core.xml + app.xml): title,
    author, organization, dates, language... Client criteria: retain all
    metadata by default -- it cannot be recovered after collection."""
    import zipfile
    try:
        from lxml import etree
    except ImportError:  # pragma: no cover
        import xml.etree.ElementTree as etree  # type: ignore
    props: dict = {}
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for part, wanted in (("docProps/core.xml", _CORE_PROP_TAGS),
                                 ("docProps/app.xml", _APP_PROP_TAGS)):
                if part not in names:
                    continue
                root = etree.fromstring(zf.read(part))
                for el in root.iter():
                    tag = el.tag.rsplit("}", 1)[-1]
                    if tag in wanted and el.text and el.text.strip():
                        props[tag] = el.text.strip()[:500]
    except Exception:
        pass
    return props


def _compute_task(task: dict) -> dict:
    """Pure per-file compute (validation, conversion, quality engine,
    screens) -- runs in a worker PROCESS; no registry access, plain
    dicts in and out so it pickles cleanly."""
    from ..download.validate import validate_payload
    from ..convert import convert_ppt_to_pptx
    from ..quality import classify as quality_classify
    from ..compliance.screens import run_screens

    payload = Path(task["payload"])
    out: dict = {"url_id": task["url_id"], "payload": str(payload),
                 "converted": 0, "reject": None, "format": None, "slide_count": 0}
    v = validate_payload(payload)
    if not v.ok:
        out["reject"] = f"validation:{v.reason}"
        return out
    if v.format not in task["allowed_formats"]:
        out["reject"] = f"format_not_allowed:{v.format}"
        return out
    if v.format == "ppt":
        res = convert_ppt_to_pptx(payload, Path(task["tmp_dir"]),
                                  soffice_bin=task["soffice_bin"],
                                  timeout_s=task["conv_timeout"])
        if not res.ok:
            out["reject"] = f"ppt_conversion_failed:{res.reason}"
            return out
        payload.unlink(missing_ok=True)
        payload = res.output_path
        out["payload"] = str(payload)
        out["converted"] = 1
        v = validate_payload(payload)
        if not v.ok:
            out["reject"] = f"converted_pptx_invalid:{v.reason}"
            return out
    out["format"] = v.format
    report = quality_classify(payload, thresholds=task["thresholds"],
                              image_thresholds=task["image_thresholds"],
                              ocr_ambiguous_only=task["ocr"])
    qrd = report.to_dict()
    qrd["doc_properties"] = extract_doc_properties(payload)
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
        self.ocr_ambiguous_only = bool(cfg.raw["quality"]["ocr_ambiguous_only"])
        conv = cfg.raw["conversion"]
        self.soffice_bin = conv["soffice_bin"]
        self.conversion_timeout = int(conv["timeout_s"])
        self.stats = {"deliver": 0, "review": 0, "reject": 0, "errors": 0}

    # ------------------------------------------------------------------
    def run(self, limit: int | None = None) -> dict:
        """Parallel classification: the pure compute (validate/convert/
        quality/screens) fans out to one worker process per CPU core;
        this process does all registry writes and file moves."""
        import os as _os
        from concurrent.futures import ProcessPoolExecutor
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)
        if limit is None:
            limit = int(self.cfg.raw.get("classify", {}).get("chunk_limit", 400)) or None
        rows = self.reg.urls_by_status("downloaded", limit=limit)
        log.info("classifying %d downloaded files%s", len(rows),
                 " [DRY RUN]" if self.dry_run else "")
        if not rows:
            return self.stats

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
            }))
        if not pairs:
            return self.stats

        workers = int(self.cfg.raw.get("classify", {}).get("workers", 0)) or _os.cpu_count() or 2
        workers = min(workers, len(pairs))
        if workers <= 1:
            results = map(_compute_task, (t for _, t in pairs))
            for (row, _), result in zip(pairs, results):
                self._persist_safe(row, result)
        else:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for (row, _), result in zip(pairs, pool.map(
                        _compute_task, (t for _, t in pairs), chunksize=4)):
                    self._persist_safe(row, result)
        log.info("classify done (%d workers): %s", workers, self.stats)
        return self.stats

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
            reg.update_file(row["id"], quality=report.quality, decision=report.decision,
                            quality_report=report.to_dict())
            # keep urls.status in sync for un-delivered files
            url_status = {"DELIVER": "classified", "REVIEW": "review",
                          "REJECT": "rejected"}[report.decision]
            url_row = reg.conn.execute("SELECT url_id FROM files WHERE id=?",
                                       (row["id"],)).fetchone()
            if url_row and url_row["url_id"]:
                reg.update_url(url_row["url_id"], status=url_status)
    log.info("reclassify done: %s", stats)
    return stats
