"""Quality engine data structures."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class SlideFeatures:
    """Per-slide/page feature vector, persisted to the registry as JSON.

    Re-classification after threshold changes reuses these vectors
    without re-downloading or re-parsing files, so this schema must stay
    backward-compatible (add fields with defaults; never repurpose).
    """
    index: int = 0
    text_char_count: int = 0
    bullet_count: int = 0
    native_chart_count: int = 0
    diagram_count: int = 0            # SmartArt / drawing-ML diagrams
    table_count: int = 0
    ole_spreadsheet_count: int = 0
    image_count: int = 0
    image_analytical_count: int = 0   # raster images classified as chart/diagram
    image_photo_count: int = 0
    vector_drawing_count: int = 0     # PDF: vector paths beyond decoration
    is_structural_filler: bool = False
    filler_reason: str = ""

    @property
    def analytical_object_count(self) -> int:
        return (self.native_chart_count + self.diagram_count + self.table_count
                + self.ole_spreadsheet_count + self.image_analytical_count)

    @property
    def is_analytical(self) -> bool:
        return self.analytical_object_count > 0

    @property
    def is_chart_or_diagram_page(self) -> bool:
        return (self.native_chart_count + self.diagram_count
                + self.image_analytical_count) > 0

    @property
    def is_photo_heavy(self) -> bool:
        return (self.image_photo_count >= 1
                and not self.is_analytical
                and self.text_char_count < 200)

    @property
    def is_text_only(self) -> bool:
        return (not self.is_analytical
                and self.image_count == 0
                and self.vector_drawing_count == 0)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SlideFeatures":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class QualityReport:
    """Result of classify(file_path)."""
    quality: str = "LOW"              # HIGH | MEDIUM | LOW
    decision: str = "REJECT"          # DELIVER | REVIEW | REJECT
    slide_count: int = 0
    format: str = ""                  # pptx | pdf
    slides: list[SlideFeatures] = field(default_factory=list)
    # Deck-level derived metrics (denominator excludes structural filler):
    content_slide_count: int = 0
    analytical_pct: float = 0.0
    chart_diagram_pages: int = 0
    photo_heavy_pct: float = 0.0
    text_only_pct: float = 0.0
    borderline: bool = False
    explanations: list[str] = field(default_factory=list)
    error: str = ""
    full_text: str = ""   # extracted text for compliance screens; not persisted

    def to_dict(self) -> dict:
        d = asdict(self)
        d["slides"] = [s if isinstance(s, dict) else asdict(s) for s in self.slides]
        d.pop("full_text", None)
        return d

    def feature_vectors_json(self) -> list[dict]:
        return [s.to_dict() for s in self.slides]
