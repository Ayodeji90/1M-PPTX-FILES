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
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image

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


def hamming_distance(a: str, b: str) -> int:
    """Number of differing bits between two hex dHash strings."""
    if len(a) != len(b):
        return _HASH_BITS
    return bin(int(a, 16) ^ int(b, 16)).count("1")
