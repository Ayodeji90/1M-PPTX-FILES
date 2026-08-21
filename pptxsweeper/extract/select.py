"""Graphical-page selection for image delivery.

The client's deliverables are *graphical pages* -- analytical graphics
with data or valuable visual content (charts, diagrams, tables,
infographic-style pages). Pure photo/exhibit pages and text-only pages
are non-compliant, and structural filler (title/agenda/thank-you) is
never a deliverable.

Selection is a PURE function of the per-slide feature vectors that
classify already persists (SlideFeatures JSON) -- no file access, so it
can be re-run cheaply after threshold changes and audited offline.

v2: Stricter filtering to avoid text-heavy slides with decorative charts,
table dumps, and pages where analytical content is a small fraction.
"""
from __future__ import annotations

# Maximum text characters allowed on a graphical page.
# Pages with more text than this are likely text-heavy with decoration.
_MAX_TEXT_CHARS = 400

# Maximum bullets allowed on a graphical page.
_MAX_BULLETS = 3


def _feat(v: dict) -> dict:
    return v or {}


def is_graphical_page(v: dict) -> tuple[bool, str]:
    """(is_graphical, reason) for one slide's feature vector.

    Graphical = analytical content (native chart / diagram / table /
    OLE spreadsheet / chart-as-image raster) that is:
    - NOT photo-heavy and NOT text-only and NOT structural filler
    - Has sufficient analytical content relative to text
    - Is not a table/spreadsheet dump
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
    text_chars = int(feats.get("text_char_count") or 0)
    bullets = int(feats.get("bullet_count") or 0)

    analytical = native + diagram + table + ole + img_analytical

    # No analytical content at all
    if analytical == 0 and vector == 0:
        return False, "no_analytical_content"

    # Photo-heavy pages (exhibits, stock photos)
    if feats.get("is_photo_heavy"):
        return False, "photo_heavy"

    # Pure text pages (no images, no charts)
    if feats.get("is_text_only"):
        return False, "text_only"

    # STRICT: Table/spreadsheet dumps -- table with lots of text is a data
    # dump, not a graphical deliverable
    if table >= 1 and ole >= 1 and text_chars > 500:
        return False, "spreadsheet_dump"

    # STRICT: Too much text -- if the page has 400+ chars of text, it's
    # a text page with decoration, not a graphical page
    if text_chars > _MAX_TEXT_CHARS and bullets > _MAX_BULLETS:
        return False, "text_heavy_with_bullets"

    # STRICT: Single analytical image with lots of text -- decorative icon
    if analytical == 1 and img_analytical == 1 and native == 0 and diagram == 0:
        if text_chars > _MAX_TEXT_CHARS:
            return False, "decorative_image_with_text"

    # STRICT: Table dump with moderate text
    if table >= 1 and native == 0 and diagram == 0 and ole == 0 and img_analytical == 0:
        if text_chars > 800:
            return False, "table_text_dump"

    # STRICT: Must have at least 1 strong analytical element (chart, diagram,
    # or 2+ analytical images) -- single tiny image doesn't qualify
    strong_analytical = native + diagram
    if strong_analytical == 0 and img_analytical < 2 and ole == 0:
        # Only has 1 analytical image -- check if it's substantial
        if text_chars > 200:
            return False, "weak_analytical_with_text"

    # PASS: This page has meaningful analytical content
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
