"""Structural-filler slide detection.

Title / agenda / section-divider / thank-you slides are excluded from
the denominator when computing analytical-page percentage, so a deck is
not penalized for having a normal presentation skeleton.

A slide is filler when it has NO analytical objects and either:
- it is the first or last slide and is text-light, or
- its text matches a filler pattern (agenda, thank you, Q&A, divider),
  or
- it is a near-empty divider (very little text, nothing else on it).
"""
from __future__ import annotations

import re

from .report import SlideFeatures

FILLER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("agenda", re.compile(r"^\s*(agenda|outline|contents?|table of contents|"
                          r"today'?s (topics|agenda)|overview of (the )?presentation)\s*$",
                          re.IGNORECASE | re.MULTILINE)),
    ("thank_you", re.compile(r"\b(thank\s*you|thanks|merci|gracias|danke|obrigado|谢谢|ありがとう)\b[\s!.]*$",
                             re.IGNORECASE)),
    ("qa", re.compile(r"^\s*(q\s*&\s*a|questions\??|any questions\??|discussion)\s*$",
                      re.IGNORECASE | re.MULTILINE)),
    ("divider", re.compile(r"^\s*(section|part|chapter|appendix)\s*[:\-]?\s*[\divxIVX]*\s*$",
                           re.IGNORECASE | re.MULTILINE)),
    ("disclaimer", re.compile(r"\b(safe harbor|forward.looking statements?|disclaimer|"
                              r"legal notice|cautionary statement)\b", re.IGNORECASE)),
]

_TEXT_LIGHT = 200   # chars; a title slide is short
_NEAR_EMPTY = 40


def detect_filler(feats: SlideFeatures, slide_text: str, slide_index: int,
                  total_slides: int) -> tuple[bool, str]:
    if feats.analytical_object_count > 0:
        return False, ""
    text = slide_text.strip()

    for reason, pattern in FILLER_PATTERNS:
        if pattern.search(text):
            return True, reason

    is_edge = slide_index == 0 or slide_index == total_slides - 1
    if is_edge and feats.text_char_count <= _TEXT_LIGHT and feats.image_photo_count <= 1:
        return True, "title_or_closing"

    if feats.text_char_count <= _NEAR_EMPTY and feats.image_count == 0 \
            and feats.vector_drawing_count == 0:
        return True, "near_empty_divider"

    return False, ""


def mark_fillers(features: list[SlideFeatures], texts: list[str]) -> None:
    total = len(features)
    for feats, text in zip(features, texts):
        is_filler, reason = detect_filler(feats, text, feats.index, total)
        feats.is_structural_filler = is_filler
        feats.filler_reason = reason
