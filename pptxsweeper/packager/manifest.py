"""Per-batch manifest CSV -- the contractual traceability backbone.

One row per delivered file. Deterministically regenerable from the
registry at any time (crash mid-upload just rewrites it).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

MANIFEST_COLUMNS = [
    "delivered_filename", "sha256", "source_url", "source_domain",
    "download_url", "original_filename", "format", "converted_from_ppt",
    "slide_count", "quality_class", "collection_ts", "download_ts",
    "http_status", "robots_status", "retrieval_method",
    "public_access_status",
    "screen_pirate", "screen_robots", "screen_rights", "screen_pii",
    "screen_minors", "screen_prohibited",
    # Image delivery (per-page PNG): which page of which source file this
    # image is, its own sha256, its perceptual hash, and how it was made.
    "page_index", "image_sha256", "phash", "source_file_sha256",
    "extraction_method",
    "final_status",
]


def public_access_status(retrieval_method: str | None, http_status) -> str:
    if retrieval_method == "wayback":
        return "archived-via-wayback"
    if http_status == 200:
        return "reachable"
    return "dead"


def manifest_row(file_row: dict) -> dict:
    """Build one manifest row from a joined files+urls row (compose.py query)."""
    try:
        compliance = json.loads(file_row.get("compliance") or "{}")
    except ValueError:
        compliance = {}
    try:
        url_meta = json.loads(file_row.get("url_metadata") or "{}")
    except ValueError:
        url_meta = {}

    retrieval = file_row.get("retrieval_method") or "origin"
    download_url = url_meta.get("wayback_snapshot_url") or file_row.get("source_url")

    # Image delivery rows: the page-level identity (which page of which
    # source file) and the image's own hashes. Deck rows leave these None.
    page_index = file_row.get("page_index")
    if page_index is not None:
        return {
            "delivered_filename": file_row.get("delivered_filename"),
            "sha256": file_row.get("image_sha256") or file_row.get("sha256"),
            "source_url": file_row.get("source_url"),
            "source_domain": file_row.get("source_domain"),
            "download_url": download_url,
            "original_filename": file_row.get("original_filename"),
            "format": "png",
            "converted_from_ppt": 0,
            "slide_count": file_row.get("page_index"),
            "quality_class": file_row.get("quality"),
            "collection_ts": file_row.get("collection_ts"),
            "download_ts": url_meta.get("download_ts"),
            "http_status": file_row.get("http_status"),
            "robots_status": file_row.get("robots_status"),
            "retrieval_method": retrieval,
            "public_access_status": public_access_status(retrieval, file_row.get("http_status")),
            "screen_pirate": compliance.get("pirate", "PASS"),
            "screen_robots": compliance.get("robots", "PASS"),
            "screen_rights": compliance.get("rights", "PASS"),
            "screen_pii": compliance.get("pii", "PASS"),
            "screen_minors": compliance.get("minors", "PASS"),
            "screen_prohibited": compliance.get("prohibited", "PASS"),
            "page_index": file_row.get("page_index"),
            "image_sha256": file_row.get("image_sha256") or file_row.get("sha256"),
            "phash": file_row.get("phash"),
            "source_file_sha256": file_row.get("source_file_sha256"),
            "extraction_method": file_row.get("extraction_method") or "libreoffice",
            "final_status": "delivered",
        }

    return {
        "delivered_filename": file_row.get("delivered_filename"),
        "sha256": file_row.get("sha256"),
        "source_url": file_row.get("source_url"),
        "source_domain": file_row.get("source_domain"),
        "download_url": download_url,
        "original_filename": file_row.get("original_filename"),
        "format": "pptx" if file_row.get("converted_from_ppt") else file_row.get("format"),
        "converted_from_ppt": int(file_row.get("converted_from_ppt") or 0),
        "slide_count": file_row.get("slide_count"),
        "quality_class": file_row.get("quality"),
        "collection_ts": file_row.get("collection_ts"),
        "download_ts": url_meta.get("download_ts"),
        "http_status": file_row.get("http_status"),
        "robots_status": file_row.get("robots_status"),
        "retrieval_method": retrieval,
        "public_access_status": public_access_status(retrieval, file_row.get("http_status")),
        "screen_pirate": compliance.get("pirate", "PASS"),
        "screen_robots": compliance.get("robots", "PASS"),
        "screen_rights": compliance.get("rights", "PASS"),
        "screen_pii": compliance.get("pii", "PASS"),
        "screen_minors": compliance.get("minors", "PASS"),
        "screen_prohibited": compliance.get("prohibited", "PASS"),
        "page_index": None,
        "image_sha256": None,
        "phash": None,
        "source_file_sha256": None,
        "extraction_method": None,
        "final_status": "delivered",
    }


def metadata_record(file_row: dict) -> dict:
    """Full per-file sidecar record: the manifest row plus quality
    explanations/metrics, OOXML doc properties, and the raw crawl
    metadata. Shared by delivered-file and review-file sidecars."""
    record = manifest_row(file_row)
    try:
        quality_report = json.loads(file_row.get("quality_report") or "{}")
    except (ValueError, TypeError):
        quality_report = {}
    record["quality_explanations"] = quality_report.get("explanations", [])
    record["quality_metrics"] = {
        k: quality_report.get(k) for k in
        ("analytical_pct", "chart_diagram_pages", "photo_heavy_pct",
         "text_only_pct", "content_slide_count")
    }
    record["doc_properties"] = quality_report.get("doc_properties", {})
    try:
        record["raw_metadata"] = json.loads(file_row.get("url_metadata") or "{}")
    except (ValueError, TypeError):
        record["raw_metadata"] = {}
    return record


def write_manifest(rows: list[dict], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: r["delivered_filename"] or ""):
            writer.writerow(row)
    return out_path
