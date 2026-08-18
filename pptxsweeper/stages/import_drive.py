"""Import decks from a Drive conversion folder into the image pipeline.

Phase-1 decks delivered as whole files were classified BEFORE delivery,
so their per-slide feature vectors still exist in the VM registries that
downloaded them. `tools/export_vectors.py` dumps those (sha256 ->
vectors + quality) from each VM. This stage downloads decks + their
`.metadata.json` provenance sidecars from a Drive folder (the moved
"old account" half), looks up the pre-computed vectors by sha256, and
registers url + files rows with `decision=DELIVER` and vectors
pre-filled -- extract then picks them up DIRECTLY, skipping the entire
classify pass (the CPU-heavy part of the pipeline).

Decks whose sha256 is not in the vectors index fall back to the normal
path: url row with status='downloaded' (payload present) so the next
classify cycle computes vectors from the payload.

Idempotent: decks already present in `files` (by sha256) are skipped;
imports are chunk-limited and disk-guarded.
"""
from __future__ import annotations

import gzip
import json
import logging
import shutil
import sqlite3
from pathlib import Path

from ..config import Config
from ..db.dao import Registry
from ..utils.disk import free_gb

log = logging.getLogger("pptxsweeper.import_drive")


def build_vectors_index(gz_path: Path, idx_path: Path) -> int:
    """Build (or refresh) a sqlite index of sha256 -> vectors/quality.

    The export is a gzip JSONL (one row per classified file). Indexing it
    once into sqlite avoids re-reading a ~100MB+ gzip every cycle.
    Returns the number of rows indexed.
    """
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = idx_path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(str(tmp))
    conn.execute("CREATE TABLE vectors (sha256 TEXT PRIMARY KEY, row TEXT NOT NULL)")
    n = 0
    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        with conn:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                sha = obj.get("sha256")
                if not sha:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO vectors (sha256, row) VALUES (?,?)",
                    (sha, json.dumps(obj)))
                n += 1
    conn.commit()
    conn.close()
    tmp.replace(idx_path)
    return n


class ImportDriveStage:
    """Download decks from a Drive folder and register them for extraction."""

    def __init__(self, cfg: Config, reg: Registry, dry_run: bool = False):
        self.cfg = cfg
        self.reg = reg
        self.dry_run = dry_run
        imp = cfg.raw.get("delivery", {}).get("import_drive", {})
        self.remote = imp.get("remote") or cfg.rclone_remote()
        self.folder = imp.get("folder", "PptxSweeper_Conversion")
        self.vectors_file = Path(imp.get("vectors_file", "data/drive_vectors.jsonl.gz"))
        self.chunk = int(imp.get("chunk_limit", 100))
        self.min_free_gb = float(imp.get("min_free_disk_gb", 2))
        self.tmp_dir = cfg.path("paths", "download_tmp_dir")
        self.work_dir = cfg.path("paths", "staging_dir") / "drive_import"
        self._idx_path = self.tmp_dir / "drive_vectors_idx.sqlite"

    # ------------------------------------------------------------------
    def _rclone(self):
        from ..packager.rclone import Rclone
        rc = self.cfg.raw["rclone"]
        return Rclone(bin=rc["bin"], remote=self.remote, root_folder=self.folder,
                      retries=int(rc.get("retries", 3)),
                      retry_backoff_s=list(rc.get("retry_backoff_s", [5, 30, 120])),
                      timeout=int(rc.get("timeout_s", 300)))

    def _list_decks(self, rclone) -> list[dict]:
        """All deck files under the conversion folder, recursively.

        The move tool keeps source batches intact (BATCH_01/ etc. under
        the conversion root), so decks may live in subfolders. Returns
        entries with a `Path` key (relative remote path) and `Name`.
        """
        import subprocess
        proc = subprocess.run(
            [rclone.bin, "lsjson", "-R", rclone.remote_path()],
            capture_output=True, text=True, timeout=rclone.timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"lsjson -R failed: {proc.stderr[-300:]}")
        entries = json.loads(proc.stdout or "[]")
        out = []
        for e in entries:
            name = e.get("Name") or ""
            if name.lower().endswith((".pptx", ".ppt", ".pdf")):
                e["Path"] = name          # relative to the conversion root
                e["Name"] = name.rsplit("/", 1)[-1]
                out.append(e)
        return out

    def _vectors_index(self) -> sqlite3.Connection | None:
        if not self.vectors_file.exists():
            log.warning("vectors file %s not found; imported decks will be "
                        "re-classified instead", self.vectors_file)
            return None
        if not self._idx_path.exists() or \
                self._idx_path.stat().st_mtime < self.vectors_file.stat().st_mtime:
            n = build_vectors_index(self.vectors_file, self._idx_path)
            log.info("built vectors index from %s (%d rows)", self.vectors_file, n)
        return sqlite3.connect(str(self._idx_path))

    def _download_raw(self, rclone, remote_path: str, local_path: Path) -> None:
        """Download a single file from a fully-resolved remote path
        (bypasses Rclone's root_folder nesting via raw rclone copy)."""
        import subprocess
        src = f"{rclone.remote}:{remote_path}"
        proc = subprocess.run(
            [rclone.bin, "copy", src, str(local_path.parent),
             "--no-traverse", "--transfers", "1", "--checkers", "1",
             "--retries", "3"],
            capture_output=True, text=True, timeout=rclone.timeout)
        if proc.returncode != 0:
            log.warning("rclone download failed for %s: %s", remote_path,
                        proc.stderr.strip()[-200:])

    def _lookup(self, idx: sqlite3.Connection | None, sha256: str) -> dict | None:
        if idx is None:
            return None
        row = idx.execute("SELECT row FROM vectors WHERE sha256=?", (sha256,)).fetchone()
        return json.loads(row[0]) if row else None

    # ------------------------------------------------------------------
    def run(self, limit: int | None = None) -> dict:
        stats = {"listed": 0, "imported": 0, "skipped_existing": 0,
                 "vectors_hit": 0, "vectors_miss": 0, "errors": 0}
        if limit is None:
            limit = self.chunk
        rclone = self._rclone()
        if not rclone.check_remote_configured():
            log.error("remote %s not configured; skipping drive import", self.remote)
            return stats
        self.work_dir.mkdir(parents=True, exist_ok=True)
        idx = self._vectors_index()

        decks = self._list_decks(rclone)
        stats["listed"] = len(decks)
        log.info("import-drive: %d decks in %s:%s", len(decks), self.remote, self.folder)
        if not decks:
            idx and idx.close()
            return stats

        # Existing shas -> skip (already in the pipeline on this VM).
        existing = {r[0] for r in self.reg.conn.execute(
            "SELECT sha256 FROM files WHERE sha256 IS NOT NULL")}

        for deck in decks:
            if stats["imported"] >= limit:
                break
            name = deck["Name"]
            if free_gb(self.tmp_dir) < self.min_free_gb:
                log.warning("import-drive paused: free disk %.1fGB < %.1fGB floor",
                            free_gb(self.tmp_dir), self.min_free_gb)
                break
            try:
                ok = self._import_one(rclone, idx, name, existing, stats,
                                      remote_path=deck.get("Path"))
                if ok:
                    stats["imported"] += 1
            except Exception:
                log.exception("import-drive failed for %s", name)
                stats["errors"] += 1
        idx and idx.close()
        self._cleanup_delivered_decks()
        log.info("import-drive done: %s", stats)
        return stats

    # ------------------------------------------------------------------
    def _import_one(self, rclone, idx, name: str, existing: set, stats: dict,
                    remote_path: str | None = None) -> bool:
        rp = remote_path or name
        stem = Path(name).stem
        sidecar = f"{stem}.metadata.json"
        sidecar_rp = str(Path(rp).parent / sidecar) if "/" in rp else sidecar

        local = self.work_dir / Path(name).name   # flat local name
        if not local.exists():
            self._download_raw(rclone, rp, local)
        if not local.exists():
            log.error("download produced no file for %s", name)
            stats["errors"] += 1
            return False

        side = self.work_dir / Path(sidecar).name
        if not side.exists():
            try:
                self._download_raw(rclone, sidecar_rp, side)
            except Exception:
                log.warning("no sidecar for %s; importing without provenance", name)
        meta = {}
        if side.exists():
            try:
                meta = json.loads(side.read_text())
            except ValueError:
                log.warning("unparseable sidecar %s", sidecar)

        sha = meta.get("sha256")
        if not sha:
            # No sidecar: hash the downloaded payload itself.
            from ..utils.hashing import sha256_file
            sha = sha256_file(local)
        if sha in existing:
            stats["skipped_existing"] += 1
            local.unlink(missing_ok=True)
            side.unlink(missing_ok=True)
            return False

        url = meta.get("source_url") or f"drive://{self.folder}/{name}"
        domain = meta.get("source_domain") or "drive-import"
        vectors = self._lookup(idx, sha) if idx else None

        # Provenance from the sidecar (mirrors what download/classify store).
        url_meta = {
            "drive_import": True,
            "local_path": str(local),
            "delivered_filename_phase1": meta.get("delivered_filename"),
            "download_url": meta.get("download_url"),
            "original_filename": meta.get("original_filename"),
            "retrieval_method": meta.get("retrieval_method"),
            "public_access_status": meta.get("public_access_status"),
            "wayback": (meta.get("raw_metadata") or {}).get("wayback_snapshot_url"),
        }
        if self.dry_run:
            log.info("[dry-run] would import %s (sha=%s, vectors=%s)",
                     name, sha[:12], "HIT" if vectors else "MISS")
            return True

        with self.reg.tx():
            cur = self.reg.conn.execute(
                """INSERT OR IGNORE INTO urls
                   (url, domain, tier, discovery_source, status, sha256,
                    http_status, robots_status, retrieval_method, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (url, domain, 1, "drive_import",
                 "classified" if vectors else "downloaded",
                 sha,
                 meta.get("http_status"),
                 meta.get("robots_status") or "allowed",
                 (meta.get("retrieval_method") or "origin")
                 if meta.get("retrieval_method") in ("origin", "wayback")
                 else "origin",
                 json.dumps(url_meta)))
            url_id = cur.lastrowid
            if url_id is None or url_id == 0:
                # URL already existed (INSERT OR IGNORE): let the existing
                # pipeline row own it; drop our copy.
                row = self.reg.conn.execute(
                    "SELECT id FROM urls WHERE url=?", (url,)).fetchone()
                url_id = row["id"] if row else None
                if url_id is None:
                    stats["errors"] += 1
                    return False

            fmt = meta.get("format")
            if fmt not in ("pptx", "ppt", "pdf"):
                fmt = "pptx" if name.lower().endswith(".pptx") else (
                    "ppt" if name.lower().endswith(".ppt") else "pdf")
            self.reg.conn.execute(
                """INSERT OR IGNORE INTO files
                   (url_id, sha256, original_filename, local_path, format,
                    file_size, slide_count, quality, decision, feature_vectors,
                    converted_from_ppt, compliance)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (url_id, sha,
                 meta.get("original_filename") or name,
                 str(local), fmt, local.stat().st_size,
                 meta.get("slide_count"),
                 (vectors or {}).get("quality") or meta.get("quality_class"),
                 "DELIVER",
                 json.dumps((vectors or {}).get("feature_vectors") or []),
                 int(meta.get("converted_from_ppt") or 0),
                 json.dumps((vectors or {}).get("compliance"))
                 if (vectors or {}).get("compliance") else None))
        existing.add(sha)
        if vectors:
            stats["vectors_hit"] += 1
        else:
            stats["vectors_miss"] += 1
        log.info("imported %s (sha=%s, %s)", name, sha[:12],
                 "vectors HIT -- extract direct" if vectors else "vectors MISS -- re-classify")
        return True

    # ------------------------------------------------------------------
    def _cleanup_delivered_decks(self) -> None:
        """Delete local payloads of imported decks whose pages are ALL
        terminal (delivered / duplicate / rejected) so the import workdir
        can never grow unbounded on a small disk."""
        # Any file in the import work dir whose url row reached a terminal
        # state and whose pages are all terminal.
        rows = self.reg.conn.execute(
            """SELECT f.id, f.local_path, u.status
               FROM files f JOIN urls u ON u.id = f.url_id
               WHERE f.local_path IS NOT NULL
                 AND f.local_path LIKE ?
                 AND u.status IN ('delivered','duplicate','rejected')
                 AND NOT EXISTS (
                     SELECT 1 FROM pages p WHERE p.file_id = f.id
                       AND p.status IN ('extracted','pending'))""",
            (f"{self.work_dir}%",)).fetchall()
        freed = 0
        for r in rows:
            p = Path(r["local_path"])
            try:
                if p.exists():
                    size = p.stat().st_size
                    p.unlink()
                    freed += size
                    log.info("cleaned delivered deck payload %s", p.name)
                from ..db.dao import utcnow
                with self.reg.tx():
                    self.reg.conn.execute(
                        "UPDATE files SET local_path=NULL, updated_at=? WHERE id=?",
                        (utcnow(), r["id"]))
            except OSError as exc:
                log.warning("cleanup failed for %s: %s", p, exc)
        if freed:
            log.info("import-drive cleanup freed %.1f MB", freed / 1e6)
        # Also remove orphaned local files (import failed mid-way).
        live = {str(Path(r[0]).resolve()) for r in self.reg.conn.execute(
            "SELECT local_path FROM files WHERE local_path IS NOT NULL")} if self.work_dir.is_dir() else set()
        for p in self.work_dir.glob("*.pptx"):
            try:
                if str(p.resolve()) not in live and p.stat().st_mtime < \
                        __import__("time").time() - 3600:
                    p.unlink()
            except OSError:
                pass
