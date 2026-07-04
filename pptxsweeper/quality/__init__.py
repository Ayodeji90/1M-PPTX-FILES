"""Quality classification engine.

Pure-function interface per spec:

    classify(file_path) -> QualityReport

No network, no registry access. The classify CLI stage persists the
report; `decide_from_features` re-runs the decision from stored feature
vectors after threshold changes without touching the file again.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .decide import decide_from_features, DEFAULT_THRESHOLDS
from .filler import mark_fillers
from .images import classify_image, ImageSignals
from .report import QualityReport, SlideFeatures

log = logging.getLogger("pptxsweeper.quality")

__all__ = [
    "classify", "decide_from_features", "reclassify_from_vectors",
    "QualityReport", "SlideFeatures", "ImageSignals", "classify_image",
    "DEFAULT_THRESHOLDS",
]


def classify(file_path: str | Path, thresholds: dict | None = None,
             image_thresholds: dict | None = None,
             ocr_ambiguous_only: bool = True) -> QualityReport:
    """Classify a validated .pptx or .pdf file. Never raises on bad
    content -- parse failures return a REJECT report with `error` set."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    def _image_classifier(data: bytes) -> ImageSignals:
        return classify_image(data, thresholds=image_thresholds,
                              ocr_ambiguous_only=ocr_ambiguous_only)

    try:
        if suffix == ".pptx":
            from .ooxml import parse_pptx
            features, texts, image_signals = parse_pptx(str(path), _image_classifier)
            fmt = "pptx"
        elif suffix == ".pdf":
            from .pdf import parse_pdf
            features, texts, image_signals = parse_pdf(str(path), _image_classifier)
            fmt = "pdf"
        else:
            report = QualityReport(error=f"unsupported extension {suffix}")
            report.explanations.append(f"unsupported extension {suffix}; only .pptx/.pdf classify")
            return report
    except Exception as exc:
        report = QualityReport(error=f"{type(exc).__name__}: {exc}")
        report.explanations.append(f"parse failure -> REJECT: {exc}")
        return report

    mark_fillers(features, texts)
    report = decide_from_features(features, thresholds, fmt=fmt)
    report.full_text = "\n".join(texts)

    # Attach image signal details to explanations for the audit record.
    analytical_imgs = sum(
        1 for slide_sigs in image_signals for s in slide_sigs if s.get("label") == "analytical"
    )
    ambiguous_imgs = sum(
        1 for slide_sigs in image_signals for s in slide_sigs if s.get("label") == "ambiguous"
    )
    if analytical_imgs or ambiguous_imgs:
        report.explanations.append(
            f"raster images: {analytical_imgs} classified analytical (chart-as-image), "
            f"{ambiguous_imgs} ambiguous (not counted as photos)"
        )
    return report


def reclassify_from_vectors(feature_vectors: list[dict],
                            thresholds: dict | None = None,
                            fmt: str = "") -> QualityReport:
    """Re-run the decision on stored per-slide vectors (no file access)."""
    features = [SlideFeatures.from_dict(v) for v in feature_vectors]
    return decide_from_features(features, thresholds, fmt=fmt)
