"""Disk-space guard."""
from __future__ import annotations

import shutil
from pathlib import Path


def free_gb(path: str | Path) -> float:
    usage = shutil.disk_usage(str(path))
    return usage.free / (1024 ** 3)


def has_free_space(path: str | Path, min_free_gb: float) -> bool:
    return free_gb(path) >= min_free_gb
