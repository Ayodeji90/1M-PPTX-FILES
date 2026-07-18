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

from ..db.dao import Registry, utcnow

log = logging.getLogger("pptxsweeper.review_promote")


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
