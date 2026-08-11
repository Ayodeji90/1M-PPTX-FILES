"""Batch composition selection.

Contract: >=70% HIGH, 20-30% MEDIUM, 0% LOW per batch. Surplus MEDIUM
beyond the 30% cap is held in `reserve` (urls.status) until enough HIGH
exists. MEDIUM below 20% is tolerated when the shortfall is made up by
HIGH (better-than-promised quality); the batch still records
composition_ok=1 in that case (documented in DECISIONS.md).
"""
from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass

from ..db.dao import Registry

log = logging.getLogger("pptxsweeper.packager.compose")


@dataclass
class Selection:
    high: list[sqlite3.Row]
    medium: list[sqlite3.Row]
    surplus_medium: list[sqlite3.Row]   # to be marked 'reserve'

    @property
    def files(self) -> list[sqlite3.Row]:
        return self.high + self.medium

    @property
    def count(self) -> int:
        return len(self.high) + len(self.medium)

    def composition_ok(self, high_min_pct: float, medium_max_pct: float) -> bool:
        if self.count == 0:
            return False
        high_min_pct, medium_max_pct = _safe_composition(high_min_pct, medium_max_pct)
        return (len(self.high) / self.count >= high_min_pct
                and len(self.medium) / self.count <= medium_max_pct)


def resolve_composition(comp: dict) -> tuple[float, float, float]:
    """Read the raw `batch.composition` config dict and return
    (high_min_pct, medium_min_pct, medium_max_pct).

    Enforces the client contract (>=70% HIGH, 20-30% MEDIUM, 0% LOW): a
    config that would deliver WEAKER batches than the contract -- any
    high_min_pct below 0.70 or medium_max_pct above 0.30 -- is clamped
    back to the contract values with a loud warning, instead of silently
    shipping non-compliant batches. Stricter-than-contract values (e.g.
    high_min 0.80 or medium_max 0.25) pass through.
    """
    high_min = float(comp.get("high_min_pct", 0.70))
    med_max = float(comp.get("medium_max_pct", 0.30))
    clamped = False
    if high_min < 0.70:
        high_min = 0.70
        clamped = True
    if med_max > 0.30:
        med_max = 0.30
        clamped = True
    if clamped:
        log.warning(
            "composition config would deliver batches weaker than the client "
            "contract (high_min_pct=%s, medium_max_pct=%s); clamped to "
            "0.70/0.30. Fix batch.composition in config.yaml.",
            comp.get("high_min_pct"), comp.get("medium_max_pct"))
    return high_min, float(comp.get("medium_min_pct", 0.20)), med_max


def deliverable_candidates(reg: Registry, batch_id: int | None = None) -> list[sqlite3.Row]:
    """DELIVER-decision files not yet delivered: urls in 'classified' or
    'reserve', or already assigned to `batch_id` (crash resume)."""
    q = """
        SELECT f.*, u.status AS url_status, u.url AS source_url, u.domain AS source_domain,
               u.created_at AS collection_ts, u.http_status, u.robots_status,
               u.retrieval_method, u.metadata AS url_metadata
        FROM files f JOIN urls u ON u.id = f.url_id
        WHERE f.decision='DELIVER' AND f.delivered_at IS NULL
          AND (u.status IN ('classified','reserve') {batch_clause})
        ORDER BY f.id
    """
    if batch_id is not None:
        q = q.format(batch_clause="OR f.batch_id = ?")
        return reg.conn.execute(q, (batch_id,)).fetchall()
    return reg.conn.execute(q.format(batch_clause="")).fetchall()


def _safe_composition(high_min_pct: float, medium_max_pct: float) -> tuple[float, float]:
    """Clamp degenerate composition values so the ratio math never divides
    by zero and caps stay sane. A high_min_pct of 0.0 (e.g. a hand-edited
    config) means 'no HIGH floor' -- the MEDIUM cap then collapses to the
    absolute batch cap, which is the sensible reading."""
    high_min_pct = float(high_min_pct or 0.0)
    medium_max_pct = max(0.0, min(1.0, float(medium_max_pct or 0.0)))
    return max(high_min_pct, 1e-9), medium_max_pct


def select_for_batch(candidates: list[sqlite3.Row], batch_size: int,
                     high_min_pct: float = 0.70, medium_max_pct: float = 0.30,
                     ) -> Selection:
    """Pick up to batch_size files, oldest first, honoring composition.

    Files already assigned to the open batch (crash resume) are taken
    first unconditionally -- their names are already allocated and a
    finalized batch must have no gaps.
    """
    high_min_pct, medium_max_pct = _safe_composition(high_min_pct, medium_max_pct)
    assigned = [c for c in candidates if c["delivered_filename"]]
    fresh = [c for c in candidates if not c["delivered_filename"]]

    high = [c for c in assigned if c["quality"] == "HIGH"]
    medium = [c for c in assigned if c["quality"] == "MEDIUM"]

    fresh_high = [c for c in fresh if c["quality"] == "HIGH"]
    fresh_medium = [c for c in fresh if c["quality"] == "MEDIUM"]

    # Aim for as much MEDIUM as the 30% cap allows (uses up MEDIUM supply
    # steadily instead of hoarding it in reserve), rest is HIGH. When HIGH
    # supply is short the cap shrinks with it, so even a short batch keeps
    # HIGH >= high_min_pct of what is actually selected.
    high_avail = len(high) + len(fresh_high)
    medium_cap = min(
        math.floor(batch_size * medium_max_pct),
        math.floor(high_avail * medium_max_pct / high_min_pct),
    )
    medium_target = min(medium_cap, len(medium) + len(fresh_medium))

    while len(medium) < medium_target and fresh_medium:
        if len(high) + len(medium) >= batch_size:
            break
        medium.append(fresh_medium.pop(0))
    for c in fresh_high:
        if len(high) + len(medium) >= batch_size:
            break
        high.append(c)
    # Room left only if HIGH supply ran short; MEDIUM beyond the cap
    # must NOT fill it (composition), so the batch simply stays short.
    surplus = fresh_medium

    return Selection(high=high, medium=medium, surplus_medium=surplus)
