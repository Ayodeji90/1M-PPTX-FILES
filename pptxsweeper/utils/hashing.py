"""SHA256 helpers."""
from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class StreamingSha256:
    """Compute SHA256 while streaming a download to disk."""

    def __init__(self) -> None:
        self._h = hashlib.sha256()

    def update(self, chunk: bytes) -> None:
        self._h.update(chunk)

    def hexdigest(self) -> str:
        return self._h.hexdigest()
