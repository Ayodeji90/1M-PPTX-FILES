"""Threshold decision logic: feature vectors -> HIGH/MEDIUM/LOW +
DELIVER/REVIEW/REJECT, with per-signal explanations.

Split from parsing so re-classification after threshold changes reuses
stored feature vectors without re-downloading or re-parsing files
(`decide_from_features` is the re-classification entry point).
"""
from __future__ import annotations

from .report import QualityReport, SlideFeatures

DEFAULT_THRESHOLDS = {
    "epsilon": 0.03,
    "min_slides": 5,
    "high": {
        "min_analytical_pct": 0.50,
        "min_chart_diagram_pages": 3,
        "max_photo_heavy_pct": 0.30,
    },
    "medium": {
        "min_analytical_pct": 0.40,
        "min_chart_diagram_pages": 1,
    },
    "low": {
        "text_only_pct": 0.75,
        "photo_heavy_pct": 0.50,
    },
}


def _merged(thresholds: dict | None) -> dict:
    t = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_THRESHOLDS.items()}
    for key, value in (thresholds or {}).items():
        if isinstance(value, dict) and isinstance(t.get(key), dict):
            t[key].update(value)
        else:
            t[key] = value
    return t


def _near(value: float, threshold: float, epsilon: float) -> bool:
    return abs(value - threshold) <= epsilon


def decide_from_features(features: list[SlideFeatures],
                         thresholds: dict | None = None,
                         fmt: str = "") -> QualityReport:
    t = _merged(thresholds)
    eps = float(t["epsilon"])
    report = QualityReport(slides=features, slide_count=len(features), format=fmt)

    if len(features) < int(t["min_slides"]):
        report.quality = "LOW"
        report.decision = "REJECT"
        report.explanations.append(
            f"slide/page count {len(features)} below contractual minimum {t['min_slides']}"
        )
        return report

    content = [f for f in features if not f.is_structural_filler]
    filler_count = len(features) - len(content)
    report.content_slide_count = len(content)
    if not content:
        report.quality = "LOW"
        report.decision = "REJECT"
        report.explanations.append("every slide is structural filler; no substantive content")
        return report

    n = len(content)
    analytical = sum(1 for f in content if f.is_analytical)
    chart_pages = sum(1 for f in content if f.is_chart_or_diagram_page)
    photo_heavy = sum(1 for f in content if f.is_photo_heavy)
    text_only = sum(1 for f in content if f.is_text_only)

    report.analytical_pct = analytical / n
    report.chart_diagram_pages = chart_pages
    report.photo_heavy_pct = photo_heavy / n
    report.text_only_pct = text_only / n

    report.explanations.append(
        f"{len(features)} slides ({filler_count} structural filler excluded from denominator); "
        f"analytical {analytical}/{n} = {report.analytical_pct:.0%}; "
        f"chart/diagram pages {chart_pages}; photo-heavy {report.photo_heavy_pct:.0%}; "
        f"text-only {report.text_only_pct:.0%}"
    )

    borderline_signals: list[str] = []

    def near(value: float, threshold: float, label: str) -> bool:
        if _near(value, float(threshold), eps):
            borderline_signals.append(f"{label} {value:.2f} within ±{eps} of threshold {threshold}")
            return True
        return False

    # LOW checks (any one triggers reject)
    low = t["low"]
    is_low, low_reasons = False, []
    if report.text_only_pct >= float(low["text_only_pct"]):
        is_low = True
        low_reasons.append(f"text-only pages {report.text_only_pct:.0%} >= {low['text_only_pct']:.0%}")
    if report.photo_heavy_pct >= float(low["photo_heavy_pct"]):
        is_low = True
        low_reasons.append(f"photo-heavy pages {report.photo_heavy_pct:.0%} >= {low['photo_heavy_pct']:.0%}")
    if analytical == 0:
        is_low = True
        low_reasons.append("no analytical structure anywhere in the deck")

    # HIGH checks
    high = t["high"]
    is_high = (report.analytical_pct >= float(high["min_analytical_pct"])
               and chart_pages >= int(high["min_chart_diagram_pages"])
               and report.photo_heavy_pct < float(high["max_photo_heavy_pct"]))

    # MEDIUM checks
    med = t["medium"]
    is_medium = (report.analytical_pct >= float(med["min_analytical_pct"])
                 and chart_pages >= int(med["min_chart_diagram_pages"]))

    # Borderline detection on every threshold actually near the score.
    near(report.analytical_pct, high["min_analytical_pct"], "analytical_pct(HIGH)")
    near(report.analytical_pct, med["min_analytical_pct"], "analytical_pct(MEDIUM)")
    near(report.photo_heavy_pct, high["max_photo_heavy_pct"], "photo_heavy_pct(HIGH)")
    near(report.text_only_pct, low["text_only_pct"], "text_only_pct(LOW)")
    near(report.photo_heavy_pct, low["photo_heavy_pct"], "photo_heavy_pct(LOW)")

    if is_high and not is_low:
        report.quality = "HIGH"
        report.explanations.append(
            f"HIGH: analytical {report.analytical_pct:.0%} >= {high['min_analytical_pct']:.0%}, "
            f"chart/diagram pages {chart_pages} >= {high['min_chart_diagram_pages']}, "
            f"photo-heavy {report.photo_heavy_pct:.0%} < {high['max_photo_heavy_pct']:.0%}"
        )
    elif is_medium and not is_low:
        report.quality = "MEDIUM"
        report.explanations.append(
            f"MEDIUM: analytical {report.analytical_pct:.0%} >= {med['min_analytical_pct']:.0%}, "
            f"chart/diagram pages {chart_pages} >= {med['min_chart_diagram_pages']}"
        )
    elif is_low:
        report.quality = "LOW"
        report.explanations.append("LOW: " + "; ".join(low_reasons))
    else:
        # Between MEDIUM and LOW: not analytical enough to deliver, but
        # not degenerate either -> LOW (0% LOW may ever be delivered).
        report.quality = "LOW"
        report.explanations.append(
            f"LOW: analytical {report.analytical_pct:.0%} below MEDIUM threshold "
            f"{med['min_analytical_pct']:.0%} (chart/diagram pages: {chart_pages})"
        )

    if report.quality in ("HIGH", "MEDIUM"):
        if borderline_signals:
            report.borderline = True
            report.decision = "REVIEW"
            report.explanations.append(
                "borderline -> REVIEW (never silently DELIVER): " + "; ".join(borderline_signals)
            )
        else:
            report.decision = "DELIVER"
    else:
        # LOW is always REJECT... unless the LOW verdict itself is borderline.
        if borderline_signals and analytical > 0:
            report.borderline = True
            report.decision = "REVIEW"
            report.explanations.append(
                "LOW verdict is borderline -> REVIEW: " + "; ".join(borderline_signals)
            )
        else:
            report.decision = "REJECT"

    return report
