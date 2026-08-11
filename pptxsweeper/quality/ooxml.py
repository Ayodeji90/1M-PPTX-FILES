"""OOXML-direct PPTX feature extraction.

Per spec: unzip and parse the XML directly -- do NOT rely solely on the
python-pptx object model. Detected natively:

- charts:   graphicData uri .../drawingml/2006/chart (+ MS chartex)
- diagrams: graphicData uri .../drawingml/2006/diagram (SmartArt)
- tables:   graphicData uri .../drawingml/2006/table
- OLE-embedded spreadsheets: .../presentationml/2006/ole with an Excel progId

Chart-as-image false-rejection fix: raster images that are merely the
fallback/preview bitmap of a native object (inside <mc:Fallback>, or
referenced by the chart part itself) are NEVER run through the photo
classifier -- the native chart is already counted, and counting its
preview bitmap as a "photo" was the prior generation's bug.
"""
from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from collections.abc import Callable

try:
    from lxml import etree
except ImportError:  # pragma: no cover - lxml is in requirements
    import xml.etree.ElementTree as etree  # type: ignore

from .images import ImageSignals, classify_image
from .report import SlideFeatures

log = logging.getLogger("pptxsweeper.quality.ooxml")

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_R_EMBED = f"{{{NS['r']}}}embed"
_R_ID = f"{{{NS['r']}}}id"

_CHART_URI_RE = re.compile(r"/(chart|chartex)$")
_DIAGRAM_URI_RE = re.compile(r"/diagram$")
_TABLE_URI_RE = re.compile(r"/table$")
_OLE_URI_RE = re.compile(r"/ole(object)?$", re.IGNORECASE)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".emf", ".wmf", ".webp"}

_CORE_PROP_TAGS = ("title", "creator", "subject", "description", "language",
                   "created", "modified", "keywords", "lastModifiedBy", "category")
_APP_PROP_TAGS = ("Company", "Application", "Slides", "Words", "PresentationFormat")


class PptxParseError(RuntimeError):
    pass


def _parse_xml(data: bytes):
    try:
        return etree.fromstring(data)
    except Exception as exc:
        raise PptxParseError(f"XML parse failed: {exc}") from exc


def _load_rels(zf: zipfile.ZipFile, part_path: str) -> dict[str, str]:
    """Relationship id -> absolute part path for one part."""
    folder, name = posixpath.split(part_path)
    rels_path = posixpath.join(folder, "_rels", name + ".rels")
    if rels_path not in zf.namelist():
        return {}
    root = _parse_xml(zf.read(rels_path))
    rels: dict[str, str] = {}
    for rel in root.iter(f"{{{NS['rel']}}}Relationship"):
        rid, target = rel.get("Id"), rel.get("Target", "")
        if not rid or rel.get("TargetMode") == "External":
            continue
        rels[rid] = posixpath.normpath(posixpath.join(folder, target))
    return rels


def slide_paths_in_order(zf: zipfile.ZipFile) -> list[str]:
    """Slide part paths in presentation order (sldIdLst), with fallback."""
    try:
        pres = _parse_xml(zf.read("ppt/presentation.xml"))
        rels = _load_rels(zf, "ppt/presentation.xml")
        ordered = []
        for sld in pres.iter(f"{{{NS['p']}}}sldId"):
            rid = sld.get(_R_ID)
            if rid and rid in rels:
                ordered.append(rels[rid])
        if ordered:
            return ordered
    except (KeyError, PptxParseError):
        pass
    # Fallback: numeric sort of ppt/slides/slideN.xml
    slides = [n for n in zf.namelist()
              if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    return sorted(slides, key=lambda n: int(re.search(r"(\d+)", n).group(1)))


def _localname(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _iter_with_fallback_flag(root):
    """Yield (element, inside_fallback) via manual DFS so descendants of
    <mc:Fallback> are flagged -- their images are native-object previews."""
    stack = [(root, False)]
    while stack:
        el, in_fb = stack.pop()
        yield el, in_fb
        is_fb = _localname(el.tag) == "Fallback"
        for child in reversed(list(el)):
            stack.append((child, in_fb or is_fb))


def _chart_part_image_rids(zf: zipfile.ZipFile, chart_part: str) -> set[str]:
    """Media targets referenced BY a chart part (cached previews)."""
    return {
        target for target in _load_rels(zf, chart_part).values()
        if posixpath.splitext(target)[1].lower() in _IMAGE_EXTS
    }


def _shape_text(el) -> str:
    return "".join(t.text or "" for t in el.iter(f"{{{NS['a']}}}t"))


def extract_doc_properties_from_zip(zf: zipfile.ZipFile) -> dict:
    """OOXML document properties (docProps/core.xml + app.xml): title,
    author, organization, dates, language... Client criteria: retain ALL
    metadata by default -- it cannot be recovered after collection.
    Runs while the zip is already open (classify does one open per file)."""
    props: dict = {}
    names = set(zf.namelist())
    for part, wanted in (("docProps/core.xml", _CORE_PROP_TAGS),
                         ("docProps/app.xml", _APP_PROP_TAGS)):
        if part not in names:
            continue
        try:
            root = _parse_xml(zf.read(part))
        except PptxParseError:
            continue
        for el in root.iter():
            tag = _localname(el.tag)
            if tag in wanted and el.text and el.text.strip():
                props[tag] = el.text.strip()[:500]
    return props


def _notes_text(zf: zipfile.ZipFile, slide_path: str,
                rels: dict[str, str]) -> str:
    """Speaker-notes text for a slide part, or '' if it has no notes.

    Notes slides carry much of the PII/context in real decks, so the
    compliance screens need them -- even though notes never affect the
    quality verdict or the filler heuristics (they are appended to the
    screen-only full_text, not to slide text)."""
    notes_part = next((t for t in rels.values()
                       if posixpath.basename(t).startswith("notesSlide")
                       and t.endswith(".xml")), None)
    if not notes_part or notes_part not in zf.namelist():
        return ""
    try:
        root = _parse_xml(zf.read(notes_part))
    except PptxParseError:
        return ""
    return "\n".join(t.text or "" for t in root.iter(f"{{{NS['a']}}}t"))


def extract_slide_features(
    zf: zipfile.ZipFile,
    slide_path: str,
    index: int,
    image_classifier: Callable[[bytes], ImageSignals] | None = None,
) -> tuple[SlideFeatures, str, list[dict], str]:
    """Parse one slide part. Returns (features, slide_text,
    image_signal_dicts, notes_text)."""
    feats = SlideFeatures(index=index)
    root = _parse_xml(zf.read(slide_path))
    rels = _load_rels(zf, slide_path)

    slide_text_parts: list[str] = []
    chart_preview_media: set[str] = set()   # media paths that are chart previews
    candidate_images: list[str] = []        # media paths to classify
    seen_media: set[str] = set()

    for el, in_fallback in _iter_with_fallback_flag(root):
        name = _localname(el.tag)

        if name == "graphicData" and not in_fallback:
            uri = el.get("uri", "")
            if _CHART_URI_RE.search(uri):
                feats.native_chart_count += 1
                for rid_el in el.iter():
                    rid = rid_el.get(_R_ID)
                    if rid and rid in rels:
                        chart_preview_media |= _chart_part_image_rids(zf, rels[rid])
            elif _DIAGRAM_URI_RE.search(uri):
                feats.diagram_count += 1
            elif _TABLE_URI_RE.search(uri):
                feats.table_count += 1
            elif _OLE_URI_RE.search(uri):
                progids = " ".join(
                    (ole.get("progId") or "") for ole in el.iter()
                    if _localname(ole.tag) == "oleObj"
                )
                if "excel" in progids.lower() or "sheet" in progids.lower():
                    feats.ole_spreadsheet_count += 1

        elif name == "blip":
            rid = el.get(_R_EMBED)
            if not rid or rid not in rels:
                continue
            media = rels[rid]
            if posixpath.splitext(media)[1].lower() not in _IMAGE_EXTS:
                continue
            if in_fallback:
                # Fallback bitmap of a native object: never classify as photo.
                chart_preview_media.add(media)
                continue
            if media not in seen_media:
                seen_media.add(media)
                candidate_images.append(media)

        elif name == "p":  # a:p paragraph
            text = _shape_text(el)
            if text.strip():
                slide_text_parts.append(text)
                ppr = el.find(f"{{{NS['a']}}}pPr")
                explicit_bullet = ppr is not None and (
                    ppr.find(f"{{{NS['a']}}}buChar") is not None
                    or ppr.find(f"{{{NS['a']}}}buAutoNum") is not None
                )
                no_bullet = ppr is not None and ppr.find(f"{{{NS['a']}}}buNone") is not None
                leveled = ppr is not None and ppr.get("lvl") not in (None, "0")
                if explicit_bullet or (leveled and not no_bullet):
                    feats.bullet_count += 1

    slide_text = "\n".join(slide_text_parts)
    feats.text_char_count = len(re.sub(r"\s", "", slide_text))

    image_signal_dicts: list[dict] = []
    # CPU guard: on a slide that already carries a NATIVE analytical object
    # (chart/diagram/table/embedded spreadsheet) no raster image can change
    # the verdict -- is_analytical is already True, so photos can't make it
    # photo-heavy and analytical images can't add a new signal. Skipping the
    # PIL/numpy classifier here removes the dominant classify cost on
    # chart-heavy decks. image_count is still recorded so the photo/text
    # heuristics see that images exist. (chart_as_image decks keep their
    # images classified: those slides have no native objects.)
    native_analytical = (feats.native_chart_count + feats.diagram_count
                         + feats.table_count + feats.ole_spreadsheet_count)
    classifier = image_classifier or (lambda data: classify_image(data))
    for media in candidate_images:
        if media in chart_preview_media:
            continue  # counted as native chart already; skip (bug fix)
        if native_analytical > 0:
            feats.image_count += 1
            image_signal_dicts.append({
                "media": media, "label": "unclassified",
                "reason": "native analytical object on slide; image classification skipped",
            })
            continue
        try:
            data = zf.read(media)
        except KeyError:
            continue
        feats.image_count += 1
        sig = classifier(data)
        record = sig.to_dict()
        record["media"] = media
        image_signal_dicts.append(record)
        if sig.label == "analytical":
            feats.image_analytical_count += 1
        elif sig.label == "photo":
            feats.image_photo_count += 1
        # 'ambiguous' counts as neither (never penalized as photo).

    return feats, slide_text, image_signal_dicts, _notes_text(zf, slide_path, rels)


def parse_pptx(path: str, image_classifier: Callable[[bytes], ImageSignals] | None = None,
               ) -> tuple[list[SlideFeatures], list[str], list[list[dict]],
                           list[str], dict]:
    """Full-deck extraction: features, per-slide text, per-slide image
    signals, per-slide speaker-notes text, and docProps (core+app). All
    from ONE zip open -- classify combines validation/quality/docProps
    instead of re-opening the file three times."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise PptxParseError(f"not a valid zip/OOXML file: {exc}") from exc
    with zf:
        if "ppt/presentation.xml" not in zf.namelist():
            raise PptxParseError("missing ppt/presentation.xml (not a PPTX)")
        slide_paths = slide_paths_in_order(zf)
        features, texts, signals, notes_texts = [], [], [], []
        for i, slide_path in enumerate(slide_paths):
            try:
                feats, text, sigs, notes = extract_slide_features(
                    zf, slide_path, i, image_classifier)
            except (KeyError, PptxParseError) as exc:
                log.warning("slide %s unreadable (%s); recording empty features", slide_path, exc)
                feats, text, sigs, notes = SlideFeatures(index=i), "", [], ""
            features.append(feats)
            texts.append(text)
            signals.append(sigs)
            notes_texts.append(notes)
        doc_props = extract_doc_properties_from_zip(zf)
    return features, texts, signals, notes_texts, doc_props
