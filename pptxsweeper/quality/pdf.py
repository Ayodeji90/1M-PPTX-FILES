"""PDF feature extraction via PyMuPDF (fitz).

Per spec: vector drawing density, embedded fonts vs scanned raster,
table structures, and the same multi-signal image classifier on
extracted raster images.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable

from .images import ImageSignals, classify_image
from .report import SlideFeatures

log = logging.getLogger("pptxsweeper.quality.pdf")

_MIN_VECTOR_ITEMS = 12       # paths with fewer segments than this are decoration
_AXIS_LINE_MIN = 6           # h/v line count suggesting a chart/table grid


class PdfParseError(RuntimeError):
    pass


def _classify_drawings(drawings: list[dict], page_area: float) -> tuple[int, int, int]:
    """Return (vector_drawing_count, hv_line_count, grid_cells_estimate)."""
    vector_count = 0
    hv_lines = 0
    rects = 0
    for d in drawings:
        items = d.get("items", [])
        if not items:
            continue
        seg_count = len(items)
        for kind, *geom in items:
            if kind == "l":  # line
                p1, p2 = geom[0], geom[1]
                if abs(p1.x - p2.x) < 0.5 or abs(p1.y - p2.y) < 0.5:
                    length = abs(p1.x - p2.x) + abs(p1.y - p2.y)
                    if length > 20:
                        hv_lines += 1
            elif kind == "re":  # rectangle
                rects += 1
        if seg_count >= 3:
            vector_count += 1
    return vector_count, hv_lines, rects


def extract_page_features(
    page, index: int,
    image_classifier: Callable[[bytes], ImageSignals] | None = None,
) -> tuple[SlideFeatures, str, list[dict]]:
    feats = SlideFeatures(index=index)
    text = page.get_text("text") or ""
    feats.text_char_count = len(re.sub(r"\s", "", text))
    feats.bullet_count = len(re.findall(r"^\s*[•▪◦‣·o\-\*]\s+\S", text, re.MULTILINE))

    page_area = max(1.0, page.rect.width * page.rect.height)

    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    vector_count, hv_lines, rects = _classify_drawings(drawings, page_area)
    feats.vector_drawing_count = vector_count

    # Native "chart-like" structure in PDF: dense h/v line work (axes,
    # gridlines) alongside vector paths -- typical of exported charts.
    if hv_lines >= _AXIS_LINE_MIN and vector_count >= _MIN_VECTOR_ITEMS:
        feats.native_chart_count += 1
    # Table structure: many rectangles/lines forming a grid with text.
    if rects >= 8 and feats.text_char_count > 100:
        feats.table_count += 1

    image_signal_dicts: list[dict] = []
    classifier = image_classifier or (lambda data: classify_image(data))
    doc = page.parent
    seen_xrefs: set[int] = set()
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        try:
            extracted = doc.extract_image(xref)
            data = extracted["image"]
        except Exception:
            continue
        feats.image_count += 1
        sig = classifier(data)
        record = sig.to_dict()
        record["xref"] = xref
        image_signal_dicts.append(record)
        if sig.label == "analytical":
            feats.image_analytical_count += 1
        elif sig.label == "photo":
            feats.image_photo_count += 1

    return feats, text, image_signal_dicts


def parse_pdf(path: str, image_classifier: Callable[[bytes], ImageSignals] | None = None,
              ) -> tuple[list[SlideFeatures], list[str], list[list[dict]],
                          list[str], dict]:
    """PDF feature extraction via PyMuPDF (fitz). Returns the same shape as
    ooxml.parse_pptx: (features, page_texts, image_signals, notes_texts,
    doc_properties) -- PDFs have no speaker notes (empty list) and
    doc_properties come from the document metadata."""
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover
        raise PdfParseError("PyMuPDF (fitz) is not installed") from exc

    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise PdfParseError(f"cannot open PDF: {exc}") from exc

    features, texts, signals = [], [], []
    doc_props: dict = {}
    with doc:
        if doc.needs_pass:
            raise PdfParseError("password-protected PDF")
        md = doc.metadata or {}
        for src, tag in (("title", "title"), ("author", "creator"),
                         ("subject", "subject"), ("keywords", "keywords")):
            if md.get(src):
                doc_props[tag] = str(md[src]).strip()[:500]
        for i, page in enumerate(doc):
            try:
                feats, text, sigs = extract_page_features(page, i, image_classifier)
            except Exception as exc:
                log.warning("page %d unreadable (%s); recording empty features", i, exc)
                feats, text, sigs = SlideFeatures(index=i), "", []
            features.append(feats)
            texts.append(text)
            signals.append(sigs)
    return features, texts, signals, [], doc_props
