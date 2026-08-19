"""Perceptual image hashing (dHash) for near-duplicate detection.

PPT-level dedup uses byte-exact SHA256, but image delivery needs
near-dup detection: the same chart rendered from two decks (or the same
web page captured twice) is visually identical while byte-different.
dHash is cheap (one Pillow resize + pixel compare) and robust to
re-encoding / slight size changes, which is exactly the duplicate class
the client measures.

dHash: downscale to 9x8 grayscale, compare each pixel to its right
neighbour -> 64 bits. Hamming distance between two hashes <= threshold
means 'visually the same image'. A threshold of ~10/64 catches genuine
dupes while letting distinct charts through (tuned in config).

Resolution-invariant mode: resize to a standard dimension before
hashing, so the same chart at 800x600 and 1920x1080 produces the
same hash.

Sub-image containment: detect when image A is a cropped/zoomed region
of image B using OpenCV template matching (if available).
"""
from __future__ import annotations

import logging
from io import BytesIO

from PIL import Image

log = logging.getLogger("pptxsweeper.perceptual")

_HASH_BITS = 64


def dhash(data: bytes, hash_size: int = 8) -> str:
    """64-bit dHash hex string of raw image bytes (PNG/JPEG/etc)."""
    img = Image.open(BytesIO(data)).convert("L").resize(
        (hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    px = img.load()
    bits = 0
    for y in range(hash_size):
        for x in range(hash_size):
            bits <<= 1
            if px[x, y] > px[x + 1, y]:
                bits |= 1
    return format(bits, "016x")


def dhash_invariant(data: bytes, standard_size: int = 256,
                    hash_size: int = 8) -> str:
    """Resolution-invariant dHash: resize to a standard square dimension
    before hashing, so the same chart at different resolutions produces
    the same hash. The standard_size parameter controls the intermediate
    resize (256px catches most resolution variants while staying fast).
    """
    img = Image.open(BytesIO(data)).convert("L")
    # Resize to standard square (preserves aspect ratio via LANCZOS)
    img = img.resize((standard_size, standard_size), Image.Resampling.LANCZOS)
    # Now apply standard dHash on the normalized image
    img = img.resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    px = img.load()
    bits = 0
    for y in range(hash_size):
        for x in range(hash_size):
            bits <<= 1
            if px[x, y] > px[x + 1, y]:
                bits |= 1
    return format(bits, "016x")


def hamming_distance(a: str, b: str) -> int:
    """Number of differing bits between two hex dHash strings."""
    if len(a) != len(b):
        return _HASH_BITS
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# ------------------------------------------------------------------
# Sub-image containment detection (requires OpenCV)
# ------------------------------------------------------------------

def _get_cv2():
    """Lazy-import OpenCV; returns None if not installed."""
    try:
        import cv2
        return cv2
    except ImportError:
        return None


def is_sub_image(small_data: bytes, large_data: bytes,
                  threshold: float = 0.85,
                  scales: list[float] | None = None,
                  max_ref_dim: int = 512) -> tuple[bool, float]:
    """Check if `small_data` is a cropped/zoomed region of `large_data`.

    Uses OpenCV template matching with multi-scale support.
    Returns (is_sub_image, match_score).

    Algorithm:
    1. Load both images as grayscale
    2. Resize the larger image to max_ref_dim if needed
    3. For each scale factor, resize the smaller image and run
       cv2.matchTemplate with TM_CCOEFF_NORMED
    4. If any match score > threshold, it's a sub-image

    Falls back to False if OpenCV is not installed.
    """
    cv2 = _get_cv2()
    if cv2 is None:
        return False, 0.0

    import numpy as np

    scales = scales or [0.5, 0.75, 1.0, 1.25, 1.5]

    try:
        # Decode images
        small_arr = np.frombuffer(small_data, np.uint8)
        large_arr = np.frombuffer(large_data, np.uint8)
        small_img = cv2.imdecode(small_arr, cv2.IMREAD_GRAYSCALE)
        large_img = cv2.imdecode(large_arr, cv2.IMREAD_GRAYSCALE)

        if small_img is None or large_img is None:
            return False, 0.0

        # Ensure small is actually smaller (swap if needed)
        if small_img.shape[0] > large_img.shape[0] or \
           small_img.shape[1] > large_img.shape[1]:
            small_img, large_img = large_img, small_img

        # Resize reference to max dimension for speed
        h, w = large_img.shape
        if max(h, w) > max_ref_dim:
            scale = max_ref_dim / max(h, w)
            large_img = cv2.resize(large_img, None, fx=scale, fy=scale,
                                   interpolation=cv2.INTER_AREA)

        best_score = 0.0
        for s in scales:
            # Resize template (small) to scale s
            sh, sw = small_img.shape
            new_w = int(sw * s)
            new_h = int(sh * s)
            if new_w <= 0 or new_h <= 0:
                continue
            if new_w > large_img.shape[1] or new_h > large_img.shape[0]:
                continue

            template = cv2.resize(small_img, (new_w, new_h),
                                  interpolation=cv2.INTER_AREA)

            result = cv2.matchTemplate(large_img, template,
                                       cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            best_score = max(best_score, max_val)

            if best_score >= threshold:
                return True, best_score

        return False, best_score

    except Exception as e:
        log.debug("sub-image detection failed: %s", e)
        return False, 0.0


def batch_anomaly_score(image_data_list: list[bytes],
                        color_threshold: float = 0.30,
                        noise_threshold: float = 0.25) -> dict:
    """Batch-level anomaly detection for AI-generated content.

    Analyzes a batch of images for statistical patterns that indicate
    bulk AI generation:
    1. Color histogram uniformity (AI images tend toward smooth gradients)
    2. High-frequency noise analysis (GAN artifacts in DCT domain)
    3. Per-image suspiciousness scoring

    Returns:
    {
        "anomaly_fraction": float,  # fraction of suspicious images
        "flagged": bool,            # True if above threshold
        "per_image_scores": list[float],  # 0-1 per image
        "color_uniformity_mean": float,
        "noise_pattern_mean": float,
    }
    """
    if not image_data_list:
        return {"anomaly_fraction": 0.0, "flagged": False,
                "per_image_scores": [], "color_uniformity_mean": 0.0,
                "noise_pattern_mean": 0.0}

    cv2 = _get_cv2()
    scores = []
    color_scores = []
    noise_scores = []

    for data in image_data_list:
        color_score = 0.0
        noise_score = 0.0

        try:
            img = Image.open(BytesIO(data)).convert("RGB")
            arr = np.array(img) if cv2 is None else None

            # 1. Color histogram uniformity
            # AI images have smoother, more uniform color distributions
            hist_r = img.histogram()[0:256]
            hist_g = img.histogram()[256:512]
            hist_b = img.histogram()[512:768]
            total_pixels = img.width * img.height
            if total_pixels > 0:
                # Normalized entropy: uniform distribution = 1.0
                import math
                def _entropy(hist):
                    ent = 0
                    for count in hist:
                        if count > 0:
                            p = count / total_pixels
                            ent -= p * math.log2(p)
                    return ent / 8.0  # normalize to 0-1 (max entropy = 8 for 256 bins)
                entropy = (_entropy(hist_r) + _entropy(hist_g) + _entropy(hist_b)) / 3.0
                # Low entropy = uniform distribution = suspicious
                color_score = 1.0 - entropy  # higher = more uniform = more suspicious
                # But very low entropy can also be solid-color images (legitimate)
                # so cap at a reasonable level
                color_score = min(color_score * 2, 1.0)  # scale up, cap at 1.0

            # 2. High-frequency noise pattern analysis
            if cv2 is not None:
                import numpy as np
                gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
                # DCT analysis: AI images show periodic patterns in high-freq
                small = cv2.resize(gray, (128, 128))
                float_img = np.float32(small)
                dct = cv2.dct(float_img)
                # High-frequency energy ratio (top-right corner of DCT)
                h, w = dct.shape
                high_freq = dct[h//2:, w//2:]
                total_energy = np.sum(np.abs(dct))
                if total_energy > 0:
                    hf_energy = np.sum(np.abs(high_freq)) / total_energy
                    # AI images tend to have lower high-freq energy
                    # (smoother, less natural noise)
                    noise_score = max(0, 0.5 - hf_energy) * 2  # map to 0-1
                else:
                    noise_score = 0.0

            combined = (color_score * 0.5 + noise_score * 0.5)
            scores.append(combined)
            color_scores.append(color_score)
            noise_scores.append(noise_score)

        except Exception:
            scores.append(0.0)
            color_scores.append(0.0)
            noise_scores.append(0.0)

    anomaly_count = sum(1 for s in scores if s > 0.5)
    return {
        "anomaly_fraction": anomaly_count / len(scores) if scores else 0.0,
        "flagged": anomaly_count / len(scores) > 0.15 if scores else False,
        "per_image_scores": scores,
        "color_uniformity_mean": sum(color_scores) / len(color_scores) if color_scores else 0.0,
        "noise_pattern_mean": sum(noise_scores) / len(noise_scores) if noise_scores else 0.0,
    }
