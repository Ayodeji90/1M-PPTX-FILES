"""Seed the registry with previously collected SHA256 hashes so those
files are never re-downloaded (the ~2,088-file existing catalog).

Accepted inputs (auto-detected per file):
- a CSV with a `sha256` column (any other columns ignored)
- a text file with one 64-hex-char hash per line (comments with #)
- a directory: every *.csv / *.txt inside is imported; every other file
  is hashed directly (i.e. pointing at the catalog's payload folder
  also works).
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from .db.dao import Registry
from .utils.hashing import sha256_file

log = logging.getLogger("pptxsweeper.catalog_import")

_HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")


def _hashes_from_csv(path: Path) -> list[str]:
    hashes: list[str] = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames:
            col = next((c for c in reader.fieldnames if c and c.strip().lower() == "sha256"), None)
            if col:
                hashes.extend(row[col].strip() for row in reader if row.get(col))
                return hashes
    # No sha256 column header: fall back to scanning for hex strings.
    return _hashes_from_text(path)


def _hashes_from_text(path: Path) -> list[str]:
    hashes: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.split("#", 1)[0]
            hashes.extend(_HEX64.findall(line))
    return hashes


def import_catalog(reg: Registry, path: str | Path) -> dict:
    """Import hashes from a file/directory. Returns summary counts."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    found: list[str] = []
    hashed_payloads = 0

    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            suffix = child.suffix.lower()
            if suffix == ".csv":
                found.extend(_hashes_from_csv(child))
            elif suffix in (".txt", ".tsv", ".list"):
                found.extend(_hashes_from_text(child))
            else:
                found.append(sha256_file(child))
                hashed_payloads += 1
    elif path.suffix.lower() == ".csv":
        found = _hashes_from_csv(path)
    else:
        found = _hashes_from_text(path)

    inserted = reg.add_known_hashes(found, origin="catalog_import")
    summary = {
        "hashes_found": len(found),
        "payloads_hashed": hashed_payloads,
        "newly_inserted": inserted,
        "already_known": len(set(h.lower() for h in found)) - inserted,
    }
    log.info("catalog import: %s", summary)
    return summary
