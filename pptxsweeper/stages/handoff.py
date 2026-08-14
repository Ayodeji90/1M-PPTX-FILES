"""Producer -> consumer URL handoff between machines.

VM1 (producer) discovers URLs; a deterministic fraction of its
`discovered` backlog is exported to a CSV and marked locally so VM1 never
downloads them. VM2 (consumer) imports that CSV into its own registry and
downloads/validates/delivers to its own Drive folder -- without running
any harvesters.

Selection is by a stable hash of the URL, so the same fraction always
goes to the consumer and repeated exports never re-hand the same URL
(handed-off rows leave the 'discovered' pool). Exchange goes through a
Drive `_handoff/` folder so it needs no direct machine-to-machine link.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
from pathlib import Path

from ..db.dao import Registry, utcnow

log = logging.getLogger("pptxsweeper.handoff")

_BUCKETS = 100
_HANDOFF_REASON = "handed_off"
_FIELDS = ["url", "domain", "tier", "discovery_source", "metadata"]


def _bucket(url: str) -> int:
    return int.from_bytes(hashlib.sha1(url.encode("utf-8")).digest()[:4], "big") % _BUCKETS


def export_urls(reg: Registry, fraction: float, out_path: str | Path,
                limit: int | None = None) -> dict:
    """Write the consumer's share of the discovered backlog to a CSV and
    mark those rows handed-off (status='filtered_out', reason='handed_off')
    so the producer stops considering them for download."""
    threshold = max(0, min(_BUCKETS, round(fraction * _BUCKETS)))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = reg.conn.execute(
        "SELECT id, url, domain, tier, discovery_source, metadata "
        "FROM urls WHERE status='discovered' ORDER BY id"
    ).fetchall()
    selected = [r for r in rows if _bucket(r["url"]) < threshold]
    if limit:
        selected = selected[:limit]

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_FIELDS)
        for r in selected:
            writer.writerow([r["url"], r["domain"], r["tier"],
                             r["discovery_source"], r["metadata"] or "{}"])

    now = utcnow()
    with reg.tx():
        reg.conn.executemany(
            "UPDATE urls SET status='filtered_out', reject_reason=?, updated_at=? WHERE id=?",
            [(_HANDOFF_REASON, now, r["id"]) for r in selected],
        )
    log.info("handoff export: %d/%d discovered urls -> %s (fraction=%.2f)",
             len(selected), len(rows), out_path, fraction)
    return {"scanned": len(rows), "exported": len(selected), "out": str(out_path)}


def import_urls(reg: Registry, in_path: str | Path) -> dict:
    """Import a handoff CSV into this node's registry as discovered URLs."""
    in_path = Path(in_path)
    if not in_path.exists():
        raise FileNotFoundError(in_path)
    candidates: list[dict] = []
    with open(in_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            url = (row.get("url") or "").strip()
            if not url:
                continue
            try:
                meta = json.loads(row.get("metadata") or "{}")
            except (ValueError, TypeError):
                meta = {}
            # Tag as handoff so the consumer's download gate (handoff_first)
            # can keep its OWN harvest on standby until this queue drains.
            # The original discovery_source is preserved (filter_stage's
            # pre-verified-format check depends on govdata/standards:..).
            meta["handoff"] = True
            candidates.append({
                "url": url,
                "domain": (row.get("domain") or "").strip(),
                "tier": int(row.get("tier") or 0),
                "discovery_source": row.get("discovery_source") or "handoff",
                "metadata": meta,
            })
    inserted = reg.upsert_candidates(candidates)
    log.info("handoff import: read %d, %d new from %s", len(candidates), inserted, in_path)
    return {"read": len(candidates), "new": inserted, "in": str(in_path)}
