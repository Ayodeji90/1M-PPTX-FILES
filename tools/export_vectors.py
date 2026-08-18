#!/usr/bin/env python3
"""Export per-file feature vectors + quality from a registry DB.

The old-account decks (delivered as decks in phase 1) were classified
before delivery, so their per-slide feature vectors still exist in the
VM registries that downloaded them. When we re-import those decks for
IMAGE delivery on another VM, we can reuse those vectors (keyed by the
deck's sha256) and SKIP re-classifying -- extract reads the vectors
directly. This dumps (sha256 -> vectors + quality + format) as gzip
JSONL; run on each VM that processed the old account's files and
concatenate the outputs.

Usage:
    python tools/export_vectors.py REGISTRY.DB OUT.vectors.jsonl.gz
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import sys


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    db_path, out_path = sys.argv[1], sys.argv[2]
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """SELECT sha256, feature_vectors, quality, format,
                  converted_from_ppt, slide_count, decision, compliance
           FROM files WHERE feature_vectors IS NOT NULL"""
    ).fetchall()
    n = 0
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        for sha, vectors, quality, fmt, cvt, slides, decision, compliance in rows:
            f.write(json.dumps({
                "sha256": sha,
                "feature_vectors": json.loads(vectors),
                "quality": quality,
                "format": fmt,
                "converted_from_ppt": int(cvt or 0),
                "slide_count": slides,
                "decision": decision,
                "compliance": json.loads(compliance) if compliance else None,
            }) + "\n")
            n += 1
    print(f"exported {n} files with feature vectors -> {out_path}")


if __name__ == "__main__":
    main()
