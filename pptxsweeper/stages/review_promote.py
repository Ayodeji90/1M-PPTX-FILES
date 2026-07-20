"""Promote manually-approved REVIEW files into the delivery pipeline.

Flipping a file's decision REVIEW->DELIVER and its url status
review->classified makes it a normal deliverable candidate, so the
existing streaming packager assigns it the next BATCH filename
(continuing the open batch's counter), writes its metadata sidecar, and
uploads it -- no re-download, no bespoke Drive move. The local review
payload (data/review/<sha>.<fmt>) is reused directly.

Used two ways:
  * one-off backlog promotion (`pptxsweeper promote-review`)
  * the orchestrator's 2-hourly auto-promotion loop
"""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from ..db.dao import Registry, utcnow

log = logging.getLogger("pptxsweeper.review_promote")

_REVIEW_ROW_SQL = """
    SELECT f.id AS file_id, f.url_id, f.sha256, f.format, f.converted_from_ppt,
           f.quality, f.quality_report, f.compliance, f.original_filename, f.slide_count,
           u.url AS source_url, u.domain AS source_domain, u.created_at AS collection_ts,
           u.http_status, u.robots_status, u.retrieval_method, u.metadata AS url_metadata
    FROM files f JOIN urls u ON u.id = f.url_id
    WHERE f.decision='REVIEW' AND f.delivered_at IS NULL AND u.status='review'
    ORDER BY f.id
"""


def promote_review_from_drive(cfg, reg: Registry, rclone, node=None,
                              limit: int | None = None, dry_run: bool = False) -> dict:
    """Promote pending REVIEW files whose LOCAL payloads are gone but whose
    copies still live in the Drive `_review/` folder.

    For each pending file: assign the next BATCH filename, server-side MOVE
    the payload `_review/<sha>.<ext>` -> `<BATCH>/<name>` (no re-download),
    upload a fresh full-metadata sidecar, delete the stale review sidecar,
    and mark the file delivered so the pipeline won't re-promote or reuse
    the number. Only genuinely-pending shas are touched -- the stale
    already-promoted copies in `_review/` are left alone.
    """
    from ..naming import BatchAllocator
    from ..packager.manifest import metadata_record
    from ..node import NodeIdentity

    review_folder = cfg.raw["rclone"]["review_folder"]
    allocator = BatchAllocator(reg, min_padding=int(cfg.raw["batch"]["min_padding"]),
                               node=node or NodeIdentity.from_env())
    rows = [dict(r) for r in reg.conn.execute(_REVIEW_ROW_SQL).fetchall()]
    if limit:
        rows = rows[:limit]
    if dry_run:
        return {"pending": len(rows)}
    if not rows:
        return {"pending": 0, "promoted": 0, "missing": 0}

    batch = dict(allocator.open_batch())
    batch_id, folder = batch["batch_id"], batch["folder_name"]
    tmpdir = Path(tempfile.mkdtemp(prefix="review_promote_"))
    # List the _review folder ONCE and map sha -> actual payload filename
    # (rclone can't stat a single Drive file reliably; and this avoids a
    # per-file API call). Matching by sha prefix handles .pptx vs .ppt.
    payload_by_sha: dict[str, str] = {}
    for e in rclone.lsjson(review_folder):
        name = e.get("Name", "")
        if name and not name.endswith(".metadata.json"):
            payload_by_sha[name.rsplit(".", 1)[0]] = name
    log.info("_review holds %d payloads on Drive; %d pending to place",
             len(payload_by_sha), len(rows))
    stats = {"pending": len(rows), "drive_review_payloads": len(payload_by_sha),
             "promoted": 0, "missing": 0, "errors": 0}

    for r in rows:
        sha = r["sha256"]
        payload_name = payload_by_sha.get(sha)
        # Skip (don't burn a filename) if the payload isn't in _review.
        if not payload_name:
            stats["missing"] += 1
            continue
        ext = payload_name.rsplit(".", 1)[-1]
        try:
            delivered = allocator.assign_filename(batch_id, r["file_id"], ext)
            stem = delivered.rsplit(".", 1)[0]
            r["delivered_filename"] = delivered
            sidecar = tmpdir / f"{stem}.metadata.json"
            sidecar.write_text(json.dumps(metadata_record(r), indent=2, default=str),
                               encoding="utf-8")
            rclone.moveto((review_folder, payload_name), (folder, delivered))
            rclone.copy_file(sidecar, folder, dest_name=f"{stem}.metadata.json")
            try:
                rclone.delete_file(review_folder, f"{sha}.metadata.json")
            except Exception:
                pass
            now = utcnow()
            with reg.tx():
                reg.conn.execute(
                    "UPDATE files SET delivered_at=?, delivered_filename=?, batch_id=?, "
                    "updated_at=? WHERE id=?", (now, delivered, batch_id, now, r["file_id"]))
                reg.conn.execute(
                    "UPDATE urls SET status='delivered', batch_id=?, updated_at=? WHERE id=?",
                    (batch_id, now, r["url_id"]))
            stats["promoted"] += 1
            if stats["promoted"] % 100 == 0:
                log.info("promoted %d/%d from _review", stats["promoted"], stats["pending"])
        except Exception:
            log.exception("failed promoting %s from _review", sha)
            stats["errors"] += 1
    log.info("promote-review --from-drive done: %s", stats)
    return stats


def _compliance_flagged(compliance_json: str | None) -> bool:
    """True if a PII/minors/rights screen sent this file to review (as
    opposed to a purely quality-borderline review)."""
    if not compliance_json:
        return False
    try:
        c = json.loads(compliance_json)
    except (ValueError, TypeError):
        return False
    for key in ("pii", "minors", "rights", "screen_pii", "screen_minors", "screen_rights"):
        v = c.get(key)
        if isinstance(v, dict) and (v.get("hit") or v.get("forces_review")):
            return True
        if v in ("review", "hit", "flagged", True):
            return True
    return bool(c.get("forces_review"))


def promote_review(reg: Registry, only_quality_borderline: bool = False,
                   dry_run: bool = False) -> dict:
    """Promote un-delivered REVIEW files to DELIVER.

    only_quality_borderline=True keeps compliance-flagged (PII/minors/
    rights) files in review for manual handling; the default promotes all.
    """
    rows = reg.conn.execute(
        """SELECT f.id AS file_id, f.url_id, f.compliance
           FROM files f JOIN urls u ON u.id = f.url_id
           WHERE f.decision='REVIEW' AND f.delivered_at IS NULL
             AND u.status='review'"""
    ).fetchall()

    to_promote: list[tuple[int, int | None]] = []
    skipped = 0
    for r in rows:
        if only_quality_borderline and _compliance_flagged(r["compliance"]):
            skipped += 1
            continue
        to_promote.append((r["file_id"], r["url_id"]))

    if dry_run:
        return {"eligible": len(rows), "would_promote": len(to_promote),
                "skipped_compliance": skipped}

    now = utcnow()
    with reg.tx():
        for file_id, url_id in to_promote:
            reg.conn.execute(
                "UPDATE files SET decision='DELIVER', updated_at=? WHERE id=?",
                (now, file_id))
            if url_id:
                reg.conn.execute(
                    "UPDATE urls SET status='classified', updated_at=? WHERE id=?",
                    (now, url_id))
    log.info("promoted %d review files to DELIVER (skipped %d compliance-flagged)",
             len(to_promote), skipped)
    return {"eligible": len(rows), "promoted": len(to_promote),
            "skipped_compliance": skipped}
