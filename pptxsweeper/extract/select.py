"""Graphical-page selection for image delivery.

The client's deliverables are *graphical pages* -- analytical graphics
with data or valuable visual content (charts, diagrams, tables,
infographic-style pages). Pure photo/exhibit pages and text-only pages
are non-compliant, and structural filler (title/agenda/thank-you) is
never a deliverable.

Selection is a PURE function of the per-slide feature vectors that
classify already persists (SlideFeatures JSON) -- no file access, so it
can be re-run cheaply after threshold changes and audited offline.
"""
from __future__ import annotations


def _feat(v: dict) -> dict:
    return v or {}


def is_graphical_page(v: dict) -> tuple[bool, str]:
    """(is_graphical, reason) for one slide's feature vector.

    Graphical = analytical content (native chart / diagram / table /
    OLE spreadsheet / chart-as-image raster) that is NOT photo-heavy and
    NOT text-only and NOT structural filler.
    """
    feats = _feat(v)
    if feats.get("is_structural_filler"):
        return False, "structural_filler"

    native = int(feats.get("native_chart_count") or 0)
    diagram = int(feats.get("diagram_count") or 0)
    table = int(feats.get("table_count") or 0)
    ole = int(feats.get("ole_spreadsheet_count") or 0)
    img_analytical = int(feats.get("image_analytical_count") or 0)
    vector = int(feats.get("vector_drawing_count") or 0)

    analytical = native + diagram + table + ole + img_analytical
    if analytical == 0 and vector == 0:
        return False, "no_analytical_content"

    if feats.get("is_photo_heavy"):
        return False, "photo_heavy"
    if feats.get("is_text_only"):
        return False, "text_only"

    if analytical > 0:
        return True, "analytical"
    if vector > 0:
        return True, "vector_graphics"
    return False, "no_analytical_content"


def select_graphical_pages(feature_vectors: list[dict] | None,
                           min_pages: int = 0) -> list[dict]:
    """Return [{index, reason}] for every graphical page, in order.

    `min_pages`: a deck must yield at least this many pages to be worth
    extracting at all (0 = no floor; every graphical page counts).
    """
    out: list[dict] = []
    for i, v in enumerate(feature_vectors or []):
        ok, reason = is_graphical_page(v)
        if ok:
            out.append({"index": i, "reason": reason})
    return out
