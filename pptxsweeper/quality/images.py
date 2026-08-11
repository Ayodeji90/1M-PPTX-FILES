"""Multi-signal raster image classification: chart/diagram vs photo.

This is where the prior generation's false-rejection bug lived (chart
images misclassified as decorative photos). Rules:

- NEVER classify by file extension or DPI alone.
- An image is called analytical (chart/diagram) only when independent
  signals agree:
    * low unique-color ratio (charts use a small palette)
    * high proportion of straight horizontal/vertical edges
    * axis-like line structures (long unbroken h/v runs near the
      left/bottom of the image)
    * large uniform background regions
    * embedded text density -- measured with lightweight OCR ONLY when
      the other signals are ambiguous
- Output is three-way: 'analytical' | 'photo' | 'ambiguous', with the
  signal values recorded for the audit trail. Ambiguous images are NOT
  counted as photos (that was the bug: treating uncertainty as
  'decorative photo' silently dragged chart-as-image decks into LOW).

Implementation: PIL + numpy only. OpenCV can be enabled via
quality.use_opencv for a better edge detector, but is optional.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, asdict

import numpy as np
from PIL import Image

log = logging.getLogger("pptxsweeper.quality.images")

# Decompression-bomb guard, tuned for small VMs: at 16 MP a decoded RGB
# frame is ~50 MB transient RAM per image, which is already far beyond what
# any real presentation raster needs and keeps a 1 GB classify worker safe.
Image.MAX_IMAGE_PIXELS = 16_000_000

_ANALYSIS_MAX_SIDE = 512  # downscale before analysis; signals are scale-robust
_MIN_SIDE = 32            # tiny images (icons/bullets) are decorative noise


@dataclass
class ImageSignals:
    width: int = 0
    height: int = 0
    unique_color_ratio: float = 1.0
    straight_edge_ratio: float = 0.0
    axis_score: float = 0.0
    uniform_bg_ratio: float = 0.0
    text_density: float | None = None    # None = OCR not run / unavailable
    votes_analytical: int = 0
    votes_photo: int = 0
    label: str = "ambiguous"             # analytical | photo | ambiguous
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _load_grayscale(data: bytes) -> tuple[np.ndarray, np.ndarray] | None:
    """Return (rgb_small, gray_small) as float arrays, or None if undecodable."""
    try:
        img = Image.open(io.BytesIO(data))
        # Decode at reduced size when the encoder supports it (JPEG DCT
        # downscale): avoids ever allocating the full-resolution frame in
        # RAM for big photos, which is the dominant classify memory spike.
        try:
            img.draft("RGB", (_ANALYSIS_MAX_SIDE, _ANALYSIS_MAX_SIDE))
        except Exception:
            pass
        img.load()
    except Image.DecompressionBombError:
        # Exceeds the pixel cap: treat as undecodable, never OOM a worker.
        return None
    except Exception:
        return None
    if img.width < _MIN_SIDE or img.height < _MIN_SIDE:
        return None
    img = img.convert("RGB")
    scale = _ANALYSIS_MAX_SIDE / max(img.width, img.height)
    if scale < 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                         Image.BILINEAR)
    rgb = np.asarray(img, dtype=np.float32)
    gray = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    return rgb, gray


def _unique_color_ratio(rgb: np.ndarray) -> float:
    # Quantize to 4 bits/channel; photos still occupy a large fraction of
    # this reduced space, charts collapse to a handful of buckets.
    q = (rgb.astype(np.uint8) >> 4)
    packed = (q[..., 0].astype(np.uint32) << 8) | (q[..., 1].astype(np.uint32) << 4) | q[..., 2]
    unique = len(np.unique(packed))
    return unique / min(packed.size, 4096)


def _gradients(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    return gx, gy


def _straight_edge_ratio(gx: np.ndarray, gy: np.ndarray) -> float:
    """Fraction of edge energy lying on near-horizontal/vertical gradients."""
    mag = np.hypot(gx, gy)
    threshold = max(20.0, float(np.percentile(mag, 90)))
    strong = mag >= threshold
    if strong.sum() < 50:
        return 0.0
    angle = np.abs(np.arctan2(gy[strong], gx[strong]))  # 0..pi
    # gradient perpendicular to edge: h/v edges -> angle near 0, pi/2, pi
    tol = np.deg2rad(8)
    axis_aligned = ((angle < tol) | (np.abs(angle - np.pi / 2) < tol)
                    | (np.abs(angle - np.pi) < tol))
    return float(axis_aligned.mean())


def _axis_score(gray: np.ndarray) -> float:
    """Detect axis-like structures: long unbroken dark runs along rows/cols.

    Charts typically have a vertical line in the left third and a
    horizontal line in the bottom third spanning most of the plot.
    """
    h, w = gray.shape
    dark = gray < (gray.mean() - 0.75 * gray.std() - 1e-6)

    def longest_run(vec: np.ndarray) -> int:
        best = cur = 0
        for v in vec:
            cur = cur + 1 if v else 0
            best = max(best, cur)
        return best

    col_score = 0.0
    for x in range(0, max(1, w // 3)):
        run = longest_run(dark[:, x])
        col_score = max(col_score, run / h)
    row_score = 0.0
    for y in range(max(0, 2 * h // 3), h):
        run = longest_run(dark[y, :])
        row_score = max(row_score, run / w)
    return float(min(col_score, row_score))


def _uniform_bg_ratio(gray: np.ndarray) -> float:
    """Fraction of pixels within a tight band around the modal intensity."""
    hist, edges = np.histogram(gray, bins=64, range=(0, 255))
    mode_bin = int(hist.argmax())
    center = (edges[mode_bin] + edges[mode_bin + 1]) / 2
    return float((np.abs(gray - center) <= 8).mean())


def _ocr_text_density(data: bytes) -> float | None:
    """Characters recognized per kilopixel; None if tesseract unavailable."""
    try:
        import pytesseract
        img = Image.open(io.BytesIO(data)).convert("L")
        if max(img.width, img.height) > 1200:
            scale = 1200 / max(img.width, img.height)
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        text = pytesseract.image_to_string(img, timeout=10)
    except Exception:
        return None
    chars = sum(1 for c in text if c.isalnum())
    kilopixels = (img.width * img.height) / 1000
    return chars / kilopixels if kilopixels else None


def classify_image(data: bytes, thresholds: dict | None = None,
                   ocr_ambiguous_only: bool = True) -> ImageSignals:
    """Classify raw image bytes as analytical / photo / ambiguous."""
    t = {
        "unique_color_ratio_max": 0.15,
        "straight_edge_ratio_min": 0.35,
        "uniform_bg_ratio_min": 0.25,
        "text_density_ambiguous_low": 0.03,
        "text_density_ambiguous_high": 0.15,
        **(thresholds or {}),
    }
    sig = ImageSignals()
    loaded = _load_grayscale(data)
    if loaded is None:
        sig.label = "ambiguous"
        sig.reason = "undecodable or too small; not counted as photo"
        return sig
    rgb, gray = loaded
    sig.height, sig.width = gray.shape

    sig.unique_color_ratio = round(_unique_color_ratio(rgb), 4)
    gx, gy = _gradients(gray)
    sig.straight_edge_ratio = round(_straight_edge_ratio(gx, gy), 4)
    sig.axis_score = round(_axis_score(gray), 4)
    sig.uniform_bg_ratio = round(_uniform_bg_ratio(gray), 4)

    votes_analytical = 0
    votes_photo = 0
    if sig.unique_color_ratio <= t["unique_color_ratio_max"]:
        votes_analytical += 1
    elif sig.unique_color_ratio >= 3 * t["unique_color_ratio_max"]:
        votes_photo += 1
    if sig.straight_edge_ratio >= t["straight_edge_ratio_min"]:
        votes_analytical += 1
    elif sig.straight_edge_ratio <= t["straight_edge_ratio_min"] / 3:
        votes_photo += 1
    if sig.uniform_bg_ratio >= t["uniform_bg_ratio_min"]:
        votes_analytical += 1
    elif sig.uniform_bg_ratio <= t["uniform_bg_ratio_min"] / 3:
        votes_photo += 1
    if sig.axis_score >= 0.5:
        votes_analytical += 1

    sig.votes_analytical = votes_analytical
    sig.votes_photo = votes_photo

    if votes_analytical >= 3 and votes_photo == 0:
        sig.label = "analytical"
        sig.reason = f"signals agree ({votes_analytical} analytical votes, 0 photo)"
        return sig
    if votes_photo >= 3 and votes_analytical == 0:
        sig.label = "photo"
        sig.reason = f"signals agree ({votes_photo} photo votes, 0 analytical)"
        return sig

    # Ambiguous -> lightweight OCR as tie-breaker (only here, per spec).
    if ocr_ambiguous_only:
        sig.text_density = _ocr_text_density(data)
        if sig.text_density is not None:
            if sig.text_density >= t["text_density_ambiguous_high"]:
                sig.label = "analytical"
                sig.reason = f"ambiguous signals; OCR text density {sig.text_density:.3f} indicates labeled chart/diagram"
                return sig
            if sig.text_density <= t["text_density_ambiguous_low"] and votes_photo > votes_analytical:
                sig.label = "photo"
                sig.reason = f"ambiguous signals; near-zero OCR text density {sig.text_density:.3f} and photo-leaning votes"
                return sig

    sig.label = "ambiguous"
    sig.reason = (f"signals disagree (analytical={votes_analytical}, photo={votes_photo}); "
                  "not counted as photo to avoid false rejection")
    return sig
