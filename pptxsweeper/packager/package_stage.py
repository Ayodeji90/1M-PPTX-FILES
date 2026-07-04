"""Package stage: batch assembly -> rclone upload -> verify -> finalize.

Flow per run (safe to re-run after a crash at ANY point):
 1. If a batch is mid-flight ('packing'/'uploading'), resume it:
    reconcile against Drive, re-upload only missing files, never
    re-assign a counter (assignment is idempotent per file).
 2. Otherwise select candidates under composition rules; if supply is
    insufficient for a full batch, hold (unless forced or the open batch
    exceeded max_batch_open_days).
 3. Assign sequential delivered names transactionally.
 4. Build a local batch dir (hardlinks -- no byte copying), write the
    manifest CSV, `rclone copy` (never move), `rclone check`.
 5. Mark files delivered, add manifest sha to the batch row, delete
    local payloads, back up the registry to Drive.
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..db.dao import Registry, utcnow
from ..naming import BatchAllocator, manifest_filename
from ..node import NodeIdentity
from ..utils.hashing import sha256_file
from .compose import deliverable_candidates, select_for_batch, Selection
from .manifest import manifest_row, write_manifest
from .rclone import Rclone

log = logging.getLogger("pptxsweeper.packager")


class BudgetExhausted(RuntimeError):
    pass


class PackageStage:
    def __init__(self, cfg: Config, reg: Registry, rclone: Rclone | None = None,
                 node: NodeIdentity | None = None, dry_run: bool = False):
        self.cfg = cfg
        self.reg = reg
        self.dry_run = dry_run
        self.node = node or NodeIdentity.from_env()
        rc = cfg.raw["rclone"]
        self.rclone = rclone or Rclone(
            bin=rc["bin"], remote=cfg.rclone_remote(), root_folder=rc["root_folder"],
            retries=int(cfg.raw["upload"]["max_retries"]),
            retry_backoff_s=list(cfg.raw["upload"]["retry_backoff_s"]),
        )
        self.verify_method = rc["verify_method"]
        self.batch_size = int(cfg.raw["batch"]["size"])
        comp = cfg.raw["batch"]["composition"]
        self.high_min_pct = float(comp["high_min_pct"])
        self.medium_max_pct = float(comp["medium_max_pct"])
        self.max_open_days = float(comp["max_batch_open_days"])
        self.daily_budget = int(float(cfg.raw["upload"]["daily_byte_budget_gb"]) * 1024 ** 3)
        self.build_dir = cfg.path("paths", "batch_build_dir")
        self.manifests_dir = cfg.path("paths", "manifests_dir")
        self.allocator = BatchAllocator(reg, min_padding=int(cfg.raw["batch"]["min_padding"]),
                                        node=self.node)

    # ------------------------------------------------------------------
    def run(self, force: bool = False) -> dict:
        """Package at most one batch. Returns a summary dict."""
        in_flight = self.reg.conn.execute(
            "SELECT * FROM batches WHERE state IN ('packing','uploading') "
            "ORDER BY batch_id LIMIT 1"
        ).fetchone()
        if in_flight:
            log.warning("resuming interrupted batch %s (state=%s)",
                        in_flight["folder_name"], in_flight["state"])
            return self._package_batch(dict(in_flight), resume=True, force=force)

        candidates = deliverable_candidates(self.reg)
        selection = select_for_batch(candidates, self.batch_size,
                                     self.high_min_pct, self.medium_max_pct)
        self._reserve_surplus(selection)

        open_batch = self.reg.conn.execute(
            "SELECT * FROM batches WHERE state='open' ORDER BY batch_id LIMIT 1"
        ).fetchone()

        if selection.count < self.batch_size and not force:
            age_ok = False
            if open_batch:
                created = datetime.fromisoformat(
                    open_batch["created_at"].replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
                age_ok = age_days >= self.max_open_days
            if not age_ok:
                log.info("insufficient supply for a full batch "
                         "(%d/%d deliverable: %d HIGH, %d MEDIUM, %d MEDIUM reserved); holding",
                         selection.count, self.batch_size, len(selection.high),
                         len(selection.medium), len(selection.surplus_medium))
                return {"status": "held", "available": selection.count,
                        "high": len(selection.high), "medium": len(selection.medium),
                        "reserved_medium": len(selection.surplus_medium)}
            log.warning("open batch exceeded max_batch_open_days=%s; closing short",
                        self.max_open_days)

        if selection.count == 0:
            return {"status": "empty"}

        batch = dict(self.allocator.open_batch())
        return self._package_batch(batch, selection=selection, force=force)

    # ------------------------------------------------------------------
    def _reserve_surplus(self, selection: Selection) -> None:
        updates = []
        for row in selection.surplus_medium:
            if row["url_status"] != "reserve" and row["url_id"]:
                updates.append((row["url_id"], {"status": "reserve"}))
        if updates:
            self.reg.update_urls(updates)
            log.info("moved %d surplus MEDIUM files to reserve", len(updates))

    # ------------------------------------------------------------------
    def _package_batch(self, batch: dict, selection: Selection | None = None,
                       resume: bool = False, force: bool = False) -> dict:
        batch_id = batch["batch_id"]
        folder = batch["folder_name"]

        if resume:
            candidates = deliverable_candidates(self.reg, batch_id=batch_id)
            selection = select_for_batch(candidates, self.batch_size,
                                         self.high_min_pct, self.medium_max_pct)
            self._reserve_surplus(selection)

        assert selection is not None
        files = selection.files
        if not files:
            log.warning("batch %s has no files; abandoning", folder)
            self.allocator.set_state(batch_id, "abandoned")
            return {"status": "abandoned", "batch": folder}

        if self.dry_run:
            log.info("[dry-run] would package %d files (%d HIGH, %d MEDIUM) into %s",
                     len(files), len(selection.high), len(selection.medium), folder)
            return {"status": "dry_run", "batch": folder, "files": len(files)}

        self.allocator.set_state(batch_id, "packing")

        # 1. Assign names (idempotent; skips files already named).
        for row in files:
            ext = "pptx" if row["converted_from_ppt"] else (row["format"] or "pptx")
            self.allocator.assign_filename(batch_id, row["id"], ext)

        # Re-read with names attached.
        candidates = deliverable_candidates(self.reg, batch_id=batch_id)
        rows = [dict(c) for c in candidates if c["delivered_filename"]]

        # 2. Budget check (size of payloads still to upload).
        total_bytes = sum(int(r.get("file_size") or 0) for r in rows)
        used = self.reg.budget_used_today()
        if used + total_bytes > self.daily_budget and not force:
            self.reg.log_event("budget_pause", None,
                               f"batch {folder}: need {total_bytes}, used {used}/{self.daily_budget}")
            log.warning("daily upload budget exhausted (%.1f/%.1f GB used); "
                        "batch %s stays in 'packing' and resumes automatically",
                        used / 1024**3, self.daily_budget / 1024**3, folder)
            return {"status": "budget_exhausted", "batch": folder}

        # 3. Build local batch dir with hardlinks under delivered names,
        #    plus a per-file metadata sidecar (BATCH_NN_file_NNNNN.metadata.json)
        #    so every delivered file carries its own provenance record.
        local_batch = self.build_dir / folder
        local_batch.mkdir(parents=True, exist_ok=True)
        missing_payloads = []
        for r in rows:
            self._write_metadata_sidecar(local_batch, r)
            src = Path(r["local_path"]) if r["local_path"] else None
            dst = local_batch / r["delivered_filename"]
            if dst.exists():
                continue
            if src is None or not src.exists():
                missing_payloads.append(r["delivered_filename"])
                continue
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

        if missing_payloads:
            # Crash-after-delete or lost disk: check whether they are
            # already on Drive (upload happened before the crash).
            remote_names = {e["Name"] for e in self.rclone.lsjson(folder)}
            really_missing = [n for n in missing_payloads if n not in remote_names]
            if really_missing:
                log.error("%d payloads missing locally AND on Drive: %s ... "
                          "requeueing their URLs for re-download",
                          len(really_missing), really_missing[:5])
                self._requeue_missing(rows, really_missing, batch_id)
                return {"status": "payloads_missing", "batch": folder,
                        "missing": len(really_missing)}

        # 4. Manifest (regenerated deterministically every attempt).
        manifest_rows = [manifest_row(r) for r in rows]
        manifest_path = local_batch / manifest_filename(batch_id, batch["padding_width"], self.node.node_id)
        write_manifest(manifest_rows, manifest_path)
        manifest_sha = sha256_file(manifest_path)

        # 5. Upload + verify.
        self.allocator.set_state(batch_id, "uploading")
        self.rclone.mkdir(folder)   # idempotent, per spec
        self.rclone.copy_dir(local_batch, folder)
        if not self.rclone.check(local_batch, folder, method=self.verify_method):
            log.error("verification failed for %s; batch stays 'uploading' for retry", folder)
            return {"status": "verify_failed", "batch": folder}

        # 6. Mark delivered + audit rows + budget accounting.
        now = utcnow()
        with self.reg.tx():
            for r in rows:
                self.reg.conn.execute(
                    "UPDATE files SET delivered_at=?, updated_at=? WHERE id=?",
                    (now, now, r["id"]))
                if r["url_id"]:
                    self.reg.conn.execute(
                        "UPDATE urls SET status='delivered', batch_id=?, updated_at=? WHERE id=?",
                        (batch_id, now, r["url_id"]))
                m = manifest_row(r)
                self.reg.conn.execute(
                    """INSERT INTO audit_log (file_id, batch_id, delivered_filename, sha256,
                        source_url, source_domain, download_url, original_filename, format,
                        converted_from_ppt, slide_count, quality_class, collection_ts,
                        download_ts, http_status, robots_status, retrieval_method,
                        public_access_status, screen_pirate, screen_robots, screen_rights,
                        screen_pii, screen_minors, screen_prohibited, final_status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["id"], batch_id, m["delivered_filename"], m["sha256"],
                     m["source_url"], m["source_domain"], m["download_url"],
                     m["original_filename"], m["format"], m["converted_from_ppt"],
                     m["slide_count"], m["quality_class"], m["collection_ts"],
                     m["download_ts"], m["http_status"], m["robots_status"],
                     m["retrieval_method"], m["public_access_status"],
                     m["screen_pirate"], m["screen_robots"], m["screen_rights"],
                     m["screen_pii"], m["screen_minors"], m["screen_prohibited"],
                     m["final_status"]))
        self.reg.budget_add(total_bytes)

        comp_ok = selection.composition_ok(self.high_min_pct, self.medium_max_pct)
        self.allocator.set_state(
            batch_id, "finalized",
            finalized_at=now, uploaded_at=now,
            file_count=len(rows),
            high_count=len(selection.high), medium_count=len(selection.medium),
            composition_ok=1 if comp_ok else 0,
            drive_path=self.rclone.remote_path(folder),
            manifest_sha256=manifest_sha,
        )

        # 7. Keep a local manifest copy, then delete payloads + build dir.
        shutil.copy2(manifest_path, self.manifests_dir / manifest_path.name)
        for r in rows:
            if r["local_path"]:
                Path(r["local_path"]).unlink(missing_ok=True)
                self.reg.update_file(r["id"], local_path=None)
        shutil.rmtree(local_batch, ignore_errors=True)

        # 8. Registry backup to Drive.
        try:
            self.backup_registry()
        except Exception:
            log.exception("registry backup failed (batch already finalized)")

        log.info("batch %s finalized: %d files (%d HIGH / %d MEDIUM), composition_ok=%s",
                 folder, len(rows), len(selection.high), len(selection.medium), comp_ok)
        return {"status": "finalized", "batch": folder, "files": len(rows),
                "high": len(selection.high), "medium": len(selection.medium),
                "composition_ok": comp_ok}

    # ------------------------------------------------------------------
    # Streaming delivery: upload each file the moment it qualifies.
    # Batch folders/manifests/counters stay contract-exact; the batch
    # only decides WHERE a file goes, never WHEN it uploads.
    # ------------------------------------------------------------------
    def stream_upload(self, max_files: int | None = None) -> dict:
        """Continuous delivery, bulk-transferred: every qualifying file is
        hardlinked into the open batch's build dir under its delivered
        name (+ metadata sidecar), then ONE parallel `rclone copy` pushes
        the whole set, one listing verifies sizes, and everything
        verified is marked delivered. Loops across batch rollovers."""
        stats = {"uploaded": 0, "reserved": 0, "requeued": 0,
                 "batches_finalized": 0, "status": "ok"}
        while True:
            batch = dict(self.allocator.open_batch())
            counts = self._batch_counts(batch["batch_id"])
            medium_cap = math.floor(self.batch_size * self.medium_max_pct)
            capacity = self.batch_size - counts["total"]
            candidates = deliverable_candidates(self.reg, batch_id=batch["batch_id"])

            prepared: list[dict] = []
            budget_left = self.daily_budget - self.reg.budget_used_today()
            folder = batch["folder_name"]
            local_batch = self.build_dir / folder
            local_batch.mkdir(parents=True, exist_ok=True)
            medium_in_batch = counts["medium"]

            for row in candidates:
                if len(prepared) >= capacity:
                    break
                if max_files and stats["uploaded"] + len(prepared) >= max_files:
                    break
                r = dict(row)
                if (r["quality"] == "MEDIUM" and not r["delivered_filename"]
                        and medium_in_batch >= medium_cap):
                    if r["url_status"] != "reserve" and r["url_id"]:
                        self.reg.update_url(r["url_id"], status="reserve")
                        stats["reserved"] += 1
                    continue
                size = int(r.get("file_size") or 0)
                if size > budget_left:
                    stats["status"] = "budget_exhausted"
                    break
                payload = Path(r["local_path"]) if r["local_path"] else None
                if payload is None or not payload.exists():
                    if r["url_id"]:
                        self.reg.update_url(r["url_id"], status="discovered")
                    stats["requeued"] += 1
                    continue
                if self.dry_run:
                    continue
                if not r["delivered_filename"]:
                    ext = "pptx" if r["converted_from_ppt"] else (r["format"] or "pptx")
                    r["delivered_filename"] = self.allocator.assign_filename(
                        batch["batch_id"], r["id"], ext)
                    r["batch_id"] = batch["batch_id"]
                dst = local_batch / r["delivered_filename"]
                if not dst.exists():
                    try:
                        os.link(payload, dst)
                    except OSError:
                        shutil.copy2(payload, dst)
                self._write_metadata_sidecar(local_batch, r)
                budget_left -= size
                if r["quality"] == "MEDIUM":
                    medium_in_batch += 1
                r["_payload"] = payload
                r["_size"] = payload.stat().st_size
                prepared.append(r)

            if not prepared:
                break

            # one parallel transfer for the whole set, one listing to verify
            try:
                self.rclone.copy_dir(local_batch, folder)
                listing = {e["Name"]: int(e.get("Size", -1))
                           for e in self.rclone.lsjson(folder)}
            except Exception:
                log.exception("bulk stream upload failed; will retry next cycle")
                stats["status"] = "upload_failed"
                break

            uploaded_bytes = 0
            for r in prepared:
                name = r["delivered_filename"]
                if listing.get(name) != r["_size"]:
                    log.error("size verify failed for %s; retrying next cycle", name)
                    continue
                now = utcnow()
                m = manifest_row(r)
                with self.reg.tx():
                    self.reg.conn.execute(
                        "UPDATE files SET delivered_at=?, local_path=NULL, "
                        "updated_at=? WHERE id=?", (now, now, r["id"]))
                    if r["url_id"]:
                        self.reg.conn.execute(
                            "UPDATE urls SET status='delivered', batch_id=?, "
                            "updated_at=? WHERE id=?",
                            (batch["batch_id"], now, r["url_id"]))
                    self._insert_audit(r["id"], batch["batch_id"], m)
                    self.reg.conn.execute(
                        """UPDATE batches SET file_count=file_count+1,
                               high_count=high_count + (?),
                               medium_count=medium_count + (?)
                           WHERE batch_id=?""",
                        (1 if r["quality"] == "HIGH" else 0,
                         1 if r["quality"] == "MEDIUM" else 0, batch["batch_id"]))
                uploaded_bytes += r["_size"]
                r["_payload"].unlink(missing_ok=True)
                (local_batch / name).unlink(missing_ok=True)
                stem = Path(name).stem
                (local_batch / f"{stem}.metadata.json").unlink(missing_ok=True)
                stats["uploaded"] += 1
            if uploaded_bytes:
                self.reg.budget_add(uploaded_bytes)

            counts = self._batch_counts(batch["batch_id"])
            if counts["total"] >= self.batch_size:
                self._finalize_streamed(batch, counts)
                stats["batches_finalized"] += 1
                continue    # rollover: next batch may have more supply
            break

        if any(stats[k] for k in ("uploaded", "reserved", "requeued", "batches_finalized")):
            log.info("stream upload: %s", stats)
        return stats

    def _batch_counts(self, batch_id: int) -> dict:
        row = self.reg.conn.execute(
            """SELECT COUNT(*) t,
                      SUM(CASE WHEN quality='HIGH' THEN 1 ELSE 0 END) h,
                      SUM(CASE WHEN quality='MEDIUM' THEN 1 ELSE 0 END) m
               FROM files WHERE batch_id=? AND delivered_at IS NOT NULL""",
            (batch_id,)).fetchone()
        return {"total": row["t"] or 0, "high": row["h"] or 0, "medium": row["m"] or 0}

    def _finalize_streamed(self, batch: dict, counts: dict) -> None:
        batch_id = batch["batch_id"]
        folder = batch["folder_name"]
        rows = [dict(x) for x in self.reg.conn.execute(
            """SELECT f.*, u.url AS source_url, u.domain AS source_domain,
                      u.created_at AS collection_ts, u.http_status, u.robots_status,
                      u.retrieval_method, u.metadata AS url_metadata
               FROM files f LEFT JOIN urls u ON u.id = f.url_id
               WHERE f.batch_id=? AND f.delivered_filename IS NOT NULL
                 AND f.delivered_at IS NOT NULL""", (batch_id,))]
        manifest_path = self.manifests_dir / manifest_filename(batch_id, batch["padding_width"], self.node.node_id)
        write_manifest([manifest_row(r) for r in rows], manifest_path)
        manifest_sha = sha256_file(manifest_path)
        self.rclone.copy_file(manifest_path, folder)

        total = max(1, counts["total"])
        comp_ok = (counts["high"] / total >= self.high_min_pct
                   and counts["medium"] / total <= self.medium_max_pct)
        now = utcnow()
        self.allocator.set_state(
            batch_id, "finalized", finalized_at=now, uploaded_at=now,
            composition_ok=1 if comp_ok else 0,
            drive_path=self.rclone.remote_path(folder), manifest_sha256=manifest_sha)
        shutil.rmtree(self.build_dir / folder, ignore_errors=True)
        try:
            self.backup_registry()
        except Exception:
            log.exception("registry backup failed (batch already finalized)")
        log.info("batch %s finalized (streaming): %d files (%d HIGH / %d MEDIUM)",
                 folder, counts["total"], counts["high"], counts["medium"])

    def _insert_audit(self, file_id: int, batch_id: int, m: dict) -> None:
        self.reg.conn.execute(
            """INSERT INTO audit_log (file_id, batch_id, delivered_filename, sha256,
                source_url, source_domain, download_url, original_filename, format,
                converted_from_ppt, slide_count, quality_class, collection_ts,
                download_ts, http_status, robots_status, retrieval_method,
                public_access_status, screen_pirate, screen_robots, screen_rights,
                screen_pii, screen_minors, screen_prohibited, final_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (file_id, batch_id, m["delivered_filename"], m["sha256"],
             m["source_url"], m["source_domain"], m["download_url"],
             m["original_filename"], m["format"], m["converted_from_ppt"],
             m["slide_count"], m["quality_class"], m["collection_ts"],
             m["download_ts"], m["http_status"], m["robots_status"],
             m["retrieval_method"], m["public_access_status"],
             m["screen_pirate"], m["screen_robots"], m["screen_rights"],
             m["screen_pii"], m["screen_minors"], m["screen_prohibited"],
             m["final_status"]))

    # ------------------------------------------------------------------
    def _write_metadata_sidecar(self, local_batch: Path, r: dict) -> None:
        """`BATCH_NN_file_NNNNN.metadata.json` next to each delivered file:
        the full manifest row plus quality details. Deterministic, so a
        crash-resume simply regenerates it."""
        stem = Path(r["delivered_filename"]).stem
        sidecar = local_batch / f"{stem}.metadata.json"
        record = manifest_row(r)
        try:
            quality_report = json.loads(r.get("quality_report") or "{}")
        except (ValueError, TypeError):
            quality_report = {}
        record["quality_explanations"] = quality_report.get("explanations", [])
        record["quality_metrics"] = {
            k: quality_report.get(k) for k in
            ("analytical_pct", "chart_diagram_pages", "photo_heavy_pct",
             "text_only_pct", "content_slide_count")
        }
        # Client criteria: retain ALL metadata. Document properties
        # (title/author/organization/dates) + full crawl/discovery record.
        record["doc_properties"] = quality_report.get("doc_properties", {})
        try:
            record["raw_metadata"] = json.loads(r.get("url_metadata") or "{}")
        except (ValueError, TypeError):
            record["raw_metadata"] = {}
        sidecar.write_text(json.dumps(record, indent=2, default=str),
                           encoding="utf-8")

    # ------------------------------------------------------------------
    def _requeue_missing(self, rows: list[dict], missing_names: list[str],
                         batch_id: int) -> None:
        """Payload lost before any copy reached Drive: the assigned name
        would leave a gap, so the batch cannot finalize until the file is
        re-downloaded and re-staged under the SAME delivered name."""
        missing = {n for n in missing_names}
        for r in rows:
            if r["delivered_filename"] in missing and r["url_id"]:
                self.reg.update_url(r["url_id"], status="discovered")
                self.reg.update_file(r["id"], local_path=None)
        self.reg.log_event("payload_missing", None,
                           f"batch {batch_id}: {len(missing)} files requeued")

    # ------------------------------------------------------------------
    def backup_registry(self) -> Path:
        """VACUUM INTO snapshot -> gzip -> upload to registry_backups/."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snap = self.build_dir / f"registry_node{self.node.node_id}_{ts}.db"
        gz = snap.with_suffix(".db.gz")
        self.build_dir.mkdir(parents=True, exist_ok=True)
        self.reg.conn.execute("VACUUM INTO ?", (str(snap),))
        with open(snap, "rb") as fin, gzip.open(gz, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        snap.unlink()
        backups_folder = self.cfg.raw["rclone"]["backups_folder"]
        self.rclone.mkdir(backups_folder)
        self.rclone.copy_file(gz, backups_folder)
        gz.unlink()
        log.info("registry backed up to Drive (%s)", gz.name)
        return gz
