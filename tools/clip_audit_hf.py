#!/usr/bin/env python3
"""
CLIP Audit Pipeline — HuggingFace Inference API (No Local Weights)

Classifies images as graphics-heavy (charts, graphs, maps) or non-qualifying
using OpenAI CLIP via HuggingFace's free Inference API. No GPU or model
download needed locally.

Features:
  - Downloads images from Google Drive via rclone
  - Classifies via HuggingFace CLIP API (free, no key needed for public models)
  - Creates PASS / REJECT / BORDERLINE folders with audit_log.csv each
  - Each audit_log.csv: filename, audit_date, live_url (Google Drive link)
  - Uploads sorted results back to Google Drive

Usage:
    # Interactive (prompts for Drive links):
    python tools/clip_audit_hf.py

    # With arguments:
    python tools/clip_audit_hf.py \\
        --input "https://drive.google.com/drive/folders/FOLDER_ID" \\
        --output "https://drive.google.com/drive/folders/OUTPUT_ID"

    # With rclone paths:
    python tools/clip_audit_hf.py \\
        --input "gdrive:/FIRST/part_001/files" \\
        --output "gdrive:/FIRST/audited"

    # Custom thresholds + HuggingFace token (optional, raises rate limits):
    python tools/clip_audit_hf.py \\
        --input gdrive:/batch_001/files \\
        --output gdrive:/audited \\
        --pass-threshold 0.75 \\
        --reject-threshold 0.35 \\
        --hf-token hf_xxxxx
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

# Load .env file if present (for HF_TOKEN, etc.)
try:
    from dotenv import load_dotenv
    # Search for .env in script dir, parent (project root), and cwd
    for _env_dir in [Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent, Path.cwd()]:
        _env_file = _env_dir / ".env"
        if _env_file.is_file():
            load_dotenv(_env_file, override=False)
            break
except ImportError:
    pass  # python-dotenv not installed — rely on shell env vars

# ─── Constants ──────────────────────────────────────────────────────────

HF_API_URL = "https://api-inference.huggingface.co/models/openai/clip-vit-base-patch32"

# Text prompts for zero-shot classification
GRAPHICS_PROMPTS = [
    "a chart with axes and data points",
    "a bar chart showing data",
    "a pie chart with colored segments",
    "a line graph showing trends over time",
    "a map with geographic data and regions",
    "an infographic with visual data and statistics",
    "a diagram showing a process or workflow",
    "a scatter plot with data distribution",
    "a data visualization dashboard with multiple charts",
    "a technical drawing or schematic diagram",
    "a heatmap showing data patterns and intensity",
    "a funnel chart or waterfall chart",
    "a radar chart comparing multiple variables",
    "a stacked bar chart or grouped bar chart",
    "a flowchart with decision nodes and arrows",
    "a Venn diagram comparing overlapping sets",
]

NON_GRAPHICS_PROMPTS = [
    "a slide with bullet points and text",
    "a photograph of people or scenery",
    "a title slide with large heading text",
    "a table with rows and columns of data",
    "a page with mostly text content and paragraphs",
    "a decorative image or company logo",
    "a closing slide with contact information",
    "a photo of a person speaking at an event",
    "a slide with quotes or testimonials",
    "a slide with company branding and colors",
    "a white background with small body text",
    "a screenshot of a software user interface",
    "a clip art or stock illustration",
    "a handwritten note or whiteboard photo",
]

RCLONE_BIN = "rclone"


# ─── Rclone Helpers ─────────────────────────────────────────────────────

def rclone_run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    """Run an rclone command."""
    cmd = [RCLONE_BIN] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def parse_drive_url(url: str) -> tuple[str, str]:
    """Parse a Google Drive URL into (folder_id, folder_name).

    Supports:
      https://drive.google.com/drive/folders/FOLDER_ID
      https://drive.google.com/drive/u/0/folders/FOLDER_ID?usp=sharing
    """
    if "drive.google.com" in url:
        parts = url.split("/folders/")
        if len(parts) > 1:
            folder_id = parts[-1].split("?")[0].strip("/")
            return folder_id, ""
    return "", url


def get_drive_file_url(remote: str, remote_path: str, filename: str) -> str:
    """Build a Google Drive viewing URL for a file.

    Uses rclone lsjson to get the file's ID, then constructs
    https://drive.google.com/file/d/FILE_ID/view
    """
    try:
        result = rclone_run(
            ["lsjson", f"{remote}:{remote_path}/{filename}"],
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            entries = json.loads(result.stdout)
            if entries and "ID" in entries[0]:
                return f"https://drive.google.com/file/d/{entries[0]['ID']}/view"
    except Exception:
        pass
    # Fallback: construct a search URL
    return f"https://drive.google.com/drive/folders/{remote_path}?q={filename}"


def rclone_mkdir(remote: str, path: str) -> None:
    """Create a remote folder (idempotent)."""
    rclone_run(["mkdir", f"{remote}:{path}"], timeout=60)


def rclone_copy_to_remote(local_path: str, remote: str, remote_path: str) -> None:
    """Upload a local file/folder to the remote."""
    rclone_run(
        ["copy", local_path, f"{remote}:{remote_path}",
         "--transfers", "8", "--checkers", "16",
         "--tpslimit", "8", "--tpslimit-burst", "16",
         "--progress"],
        timeout=3600,
    )


def rclone_list_files(remote: str, path: str) -> list[dict]:
    """List files in a remote folder. Returns list of dicts with Name, ID, Size."""
    try:
        result = rclone_run(
            ["lsjson", f"{remote}:{path}", "--no-modtime"],
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return []


def rclone_download_folder(remote: str, remote_path: str, local_dir: str,
                          include: str = "*.png,*.jpg,*.jpeg,*.PNG,*.JPG,*.JPEG") -> bool:
    """Download a remote folder to local directory.

    By default only downloads image files (not metadata JSONs etc).
    Timeout is generous because 14K+ files can take a while.
    """
    try:
        cmd = [
            "copy", f"{remote}:{remote_path}", local_dir,
            "--transfers", "8", "--checkers", "16",
            "--include", include,
            "--no-traverse",  # faster for known folder
            "--tpslimit", "8", "--tpslimit-burst", "16",
            "--progress",
        ]
        result = rclone_run(cmd, timeout=7200)  # 2 hour timeout for large folders
        return result.returncode == 0
    except Exception:
        return False


# ─── Drive URL Resolution ──────────────────────────────────────────────

def resolve_input_path(url_or_path: str) -> tuple[str, str, str]:
    """Resolve input to (rclone_remote, rclone_path, display_name).

    Handles:
      - Google Drive URLs -> downloads to temp, returns local path
      - rclone paths (gdrive:/foo) -> returns as-is
      - Local paths -> returns as-is
    """
    # Google Drive URL
    if "drive.google.com" in url_or_path:
        folder_id, _ = parse_drive_url(url_or_path)
        if folder_id:
            # We'll download via rclone using the folder ID
            return "gdrive", f"/{folder_id}", f"Drive:{folder_id[:12]}..."

    # rclone path (contains ":")
    if ":" in url_or_path and not url_or_path.startswith("http"):
        parts = url_or_path.split(":", 1)
        return parts[0], parts[1], url_or_path

    # Local path
    return "", url_or_path, url_or_path


# ─── CLIP Classification via HuggingFace API ────────────────────────────

class HFClipClassifier:
    """Classify images using CLIP via HuggingFace Inference API.

    No model weights loaded locally — all inference happens on HF's servers.
    """

    def __init__(self, pass_threshold: float = 0.70,
                 reject_threshold: float = 0.40,
                 hf_token: Optional[str] = None,
                 batch_size: int = 32):
        self.pass_threshold = pass_threshold
        self.reject_threshold = reject_threshold
        self.batch_size = batch_size

        # Optional HF token for higher rate limits
        self.headers = {}
        if hf_token:
            self.headers["Authorization"] = f"Bearer {hf_token}"

        # Pre-encode text features on the API (one-time call)
        self._warm_up()

    def _warm_up(self):
        """Send a test request to verify API is reachable."""
        try:
            resp = httpx.post(
                HF_API_URL,
                json={"inputs": "test"},
                headers=self.headers,
                timeout=30,
            )
            if resp.status_code == 200:
                print("  ✅ HuggingFace CLIP API connected")
            elif resp.status_code == 503:
                # Model is loading — wait and retry
                print("  ⏳ Model loading on HuggingFace... waiting 30s")
                time.sleep(30)
                resp = httpx.post(
                    HF_API_URL,
                    json={"inputs": "test"},
                    headers=self.headers,
                    timeout=30,
                )
                if resp.status_code == 200:
                    print("  ✅ HuggingFace CLIP API ready")
                else:
                    print(f"  ⚠️  API returned {resp.status_code} — proceeding anyway")
            else:
                print(f"  ⚠️  API returned {resp.status_code} — proceeding anyway")
        except Exception as e:
            print(f"  ⚠️  Could not reach HF API: {e} — will retry during classification")

    def _classify_image(self, image_path: str) -> dict:
        """Classify a single image via HuggingFace CLIP API.

        Returns dict with graphics_score, non_graphics_score, classification.
        """
        try:
            with open(image_path, "rb") as f:
                image_bytes = f.read()

            # Use the feature-extraction endpoint to get image embeddings
            # Then compute cosine similarity with text prompts
            resp = httpx.post(
                HF_API_URL,
                content=image_bytes,
                headers={**self.headers, "Content-Type": "application/octet-stream"},
                timeout=60,
            )

            if resp.status_code == 503:
                # Model loading — wait and retry once
                time.sleep(20)
                resp = httpx.post(
                    HF_API_URL,
                    content=image_bytes,
                    headers={**self.headers, "Content-Type": "application/octet-stream"},
                    timeout=60,
                )

            if resp.status_code != 200:
                return {
                    "graphics_score": 0.0,
                    "non_graphics_score": 0.0,
                    "classification": "error",
                    "reasons": f"API error {resp.status_code}: {resp.text[:200]}",
                }

            # The CLIP model returns image-text similarity scores
            # We need to send text prompts separately and compare
            # Actually, for zero-shot we need to send text too
            # Let's use the pipeline approach instead

            return self._classify_with_text_prompts(image_bytes)

        except httpx.TimeoutException:
            # API timeout — fall back to local heuristics
            return self._classify_simple(image_bytes)
        except Exception as e:
            # Network error or other failure — fall back to local heuristics
            try:
                return self._classify_simple(image_bytes)
            except Exception:
                return {
                    "graphics_score": 0.0,
                    "non_graphics_score": 0.0,
                    "classification": "error",
                    "reasons": str(e),
                }

    def _classify_with_text_prompts(self, image_bytes: bytes) -> dict:
        """Classify image by comparing against graphics vs non-graphics prompts.

        Uses the HuggingFace zero-shot classification pipeline endpoint.
        Falls back to local heuristics if the API is unreachable.
        """
        try:
            # Method: send image + candidate labels
            # CLIP can do zero-shot classification with candidate_labels
            resp = httpx.post(
                "https://api-inference.huggingface.co/pipeline/zero-shot-image-classification/openai/clip-vit-base-patch32",
                content=image_bytes,
                headers={
                    **self.headers,
                    "Content-Type": "application/octet-stream",
                },
                json={
                    "parameters": {
                        "candidate_labels": [
                            "a chart or graph",
                            "a map or geographic visualization",
                            "an infographic or data visualization",
                            "a diagram or flowchart",
                            "a text document or slide",
                            "a photograph or photo",
                            "a logo or decorative image",
                            "a table or spreadsheet",
                        ],
                    },
                },
                timeout=60,
            )

            if resp.status_code == 200:
                data = resp.json()
                labels = data.get("labels", [])
                scores = data.get("scores", [])

                # Sum up graphics-related scores
                graphics_keywords = {"chart", "graph", "map", "infographic", "diagram", "flowchart", "visualization"}
                non_graphics_keywords = {"text", "photo", "logo", "decorative", "table", "spreadsheet"}

                graphics_score = 0.0
                non_graphics_score = 0.0

                for label, score in zip(labels, scores):
                    label_lower = label.lower()
                    if any(kw in label_lower for kw in graphics_keywords):
                        graphics_score += score
                    elif any(kw in label_lower for kw in non_graphics_keywords):
                        non_graphics_score += score

                # Normalize
                total = graphics_score + non_graphics_score
                if total > 0:
                    graphics_prob = graphics_score / total
                    non_graphics_prob = non_graphics_score / total
                else:
                    graphics_prob = 0.5
                    non_graphics_prob = 0.5

                # Classify
                if graphics_prob >= self.pass_threshold:
                    classification = "pass"
                    reasons = "high_confidence_graphics" if graphics_prob > 0.85 else "moderate_confidence_graphics"
                elif graphics_prob >= self.reject_threshold:
                    classification = "borderline"
                    if graphics_prob > 0.55:
                        reasons = "leaning_graphics_but_uncertain"
                    elif graphics_prob > 0.45:
                        reasons = "even_split"
                    else:
                        reasons = "leaning_non_graphics_but_uncertain"
                else:
                    classification = "reject"
                    reasons = "clearly_non_graphics" if graphics_prob < 0.25 else "likely_non_graphics"

                return {
                    "graphics_score": round(graphics_prob, 4),
                    "non_graphics_score": round(non_graphics_prob, 4),
                    "classification": classification,
                    "reasons": reasons,
                    "raw_labels": dict(zip(labels, [round(s, 4) for s in scores])),
                }

            elif resp.status_code == 503:
                # Model loading — wait and retry once
                time.sleep(20)
                return self._classify_with_text_prompts(image_bytes)
            else:
                # API error — fall back to local heuristics
                return self._classify_simple(image_bytes)

        except Exception:
            # Network error or timeout — fall back to local heuristics
            return self._classify_simple(image_bytes)

    def _classify_simple(self, image_bytes: bytes) -> dict:
        """Fallback: simple image feature extraction + heuristics.

        Used when the zero-shot pipeline is unavailable.
        """
        from PIL import Image
        import io
        import numpy as np

        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            arr = np.array(img)

            # Simple heuristic: color variance + edge density
            # High color variance + structured edges = likely chart/graph
            gray = np.mean(arr, axis=2)

            # Edge detection via simple gradient
            gx = np.abs(np.diff(gray, axis=1))
            gy = np.abs(np.diff(gray, axis=0))
            edge_density = (np.mean(gx > 20) + np.mean(gy > 20)) / 2

            # Color variance
            color_std = np.mean([np.std(arr[:,:,i]) for i in range(3)])

            # White background ratio
            white_mask = np.all(arr > 230, axis=2)
            white_ratio = np.mean(white_mask)

            # Score based on heuristics
            score = 0.5  # baseline

            # Charts have moderate edge density (not too sparse, not too dense)
            if 0.02 < edge_density < 0.15:
                score += 0.15
            elif edge_density < 0.01:
                score -= 0.2  # likely blank or photo

            # Charts have diverse colors
            if color_std > 40:
                score += 0.15
            elif color_std < 15:
                score -= 0.1  # monochrome

            # Charts usually have some white background but not all white
            if 0.3 < white_ratio < 0.7:
                score += 0.1
            elif white_ratio > 0.85:
                score -= 0.15  # likely text slide

            score = max(0.0, min(1.0, score))

            if score >= self.pass_threshold:
                classification = "pass"
                reasons = "heuristic_graphics"
            elif score >= self.reject_threshold:
                classification = "borderline"
                reasons = "heuristic_uncertain"
            else:
                classification = "reject"
                reasons = "heuristic_non_graphics"

            return {
                "graphics_score": round(score, 4),
                "non_graphics_score": round(1 - score, 4),
                "classification": classification,
                "reasons": reasons,
            }

        except Exception as e:
            return {
                "graphics_score": 0.0,
                "non_graphics_score": 1.0,
                "classification": "reject",
                "reasons": f"heuristic_error: {e}",
            }

    def classify_batch(self, image_paths: list[str]) -> list[dict]:
        """Classify a batch of images. Returns list of result dicts."""
        results = []
        for path in image_paths:
            result = self._classify_image(path)
            result["filename"] = os.path.basename(path)
            results.append(result)
            # Rate limiting: HF free tier is ~300 req/min
            time.sleep(0.15)
        return results

    def classify_directory(self, image_dir: str) -> list[dict]:
        """Classify all PNGs in a directory."""
        png_files = sorted([
            f for f in os.listdir(image_dir)
            if f.lower().endswith((".png", ".jpg", ".jpeg"))
        ])

        if not png_files:
            print(f"  No image files found in {image_dir}")
            return []

        print(f"\n  Classifying {len(png_files)} images via HuggingFace CLIP API...\n")

        all_results = []
        start_time = time.time()

        for batch_start in range(0, len(png_files), self.batch_size):
            batch_files = png_files[batch_start:batch_start + self.batch_size]
            batch_paths = [os.path.join(image_dir, f) for f in batch_files]

            batch_results = self.classify_batch(batch_paths)
            all_results.extend(batch_results)

            done = len(all_results)
            elapsed = time.time() - start_time
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(png_files) - done) / rate if rate > 0 else 0

            batch_pass = sum(1 for r in batch_results if r["classification"] == "pass")
            batch_reject = sum(1 for r in batch_results if r["classification"] == "reject")
            batch_border = sum(1 for r in batch_results if r["classification"] == "borderline")

            print(
                f"  [{done:>5}/{len(png_files)}] "
                f"pass={batch_pass} reject={batch_reject} borderline={batch_border} | "
                f"{rate:.1f} img/sec | ETA: {eta:.0f}s"
            )

        total_time = time.time() - start_time
        print(f"\n  Done: {len(all_results)} images in {total_time:.1f}s ({len(all_results)/total_time:.1f} img/sec)")

        return all_results


# ─── Output Sorting ─────────────────────────────────────────────────────

def _read_download_url(input_dir: str, image_filename: str) -> str:
    """Read download_url from the metadata JSON for an image.

    Searches for {stem}.metadata.json in:
      1. Same directory as the image
      2. Parent directory
      3. Grandparent directory

    Returns the download_url field, or empty string if not found.
    """
    stem = os.path.splitext(image_filename)[0]
    meta_name = f"{stem}.metadata.json"

    # Search in image dir, parent, and grandparent
    search_dir = Path(input_dir)
    for _ in range(3):
        meta_path = search_dir / meta_name
        if meta_path.is_file():
            try:
                with open(meta_path, encoding="utf-8") as f:
                    data = json.load(f)
                # download_url is the live URL where the file was fetched from
                url = data.get("download_url") or data.get("source_url") or ""
                return url.strip() if url else ""
            except (json.JSONDecodeError, OSError):
                return ""
        search_dir = search_dir.parent

    return ""


def sort_and_write(
    results: list[dict],
    input_dir: str,
    output_dir: str,
    drive_remote: str = "",
    drive_folder_id: str = "",
) -> dict:
    """Sort images into PASS/REJECT/BORDERLINE folders with audit_log.csv.

    Each audit_log.csv contains: filename, audit_date, live_url
    (live_url = download_url from the image's metadata JSON)
    """
    pass_dir = os.path.join(output_dir, "PASS")
    reject_dir = os.path.join(output_dir, "REJECT")
    borderline_dir = os.path.join(output_dir, "BORDERLINE")

    for d in [pass_dir, reject_dir, borderline_dir]:
        os.makedirs(d, exist_ok=True)

    pass_list = [r for r in results if r["classification"] == "pass"]
    reject_list = [r for r in results if r["classification"] == "reject"]
    borderline_list = [r for r in results if r["classification"] == "borderline"]
    error_list = [r for r in results if r["classification"] == "error"]

    audit_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Copy images to folders and build file listings
    def process_folder(file_list: list[dict], dest_dir: str, category: str) -> list[dict]:
        copied = 0
        audit_rows = []
        for r in file_list:
            src = os.path.join(input_dir, r["filename"])
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dest_dir, r["filename"]))
                copied += 1

                # Read download_url from metadata JSON
                live_url = _read_download_url(input_dir, r["filename"])

                audit_rows.append({
                    "filename": r["filename"],
                    "audit_date": audit_date,
                    "live_url": live_url,
                })
        return audit_rows

    print("\n  Sorting images into folders...")
    pass_rows = process_folder(pass_list, pass_dir, "PASS")
    reject_rows = process_folder(reject_list, reject_dir, "REJECT")
    borderline_rows = process_folder(borderline_list, borderline_dir, "BORDERLINE")

    print(f"    PASS:       {len(pass_rows)} images → {pass_dir}")
    print(f"    REJECT:     {len(reject_rows)} images → {reject_dir}")
    print(f"    BORDERLINE: {len(borderline_rows)} images → {borderline_dir}")
    if error_list:
        print(f"    ERRORS:     {len(error_list)} (could not classify)")

    # Write audit_log.csv for each category
    def write_audit_csv(rows: list[dict], csv_path: str, category: str):
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["filename", "audit_date", "live_url"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"    📝 {category}/audit_log.csv — {len(rows)} entries")

    write_audit_csv(pass_rows, os.path.join(pass_dir, "audit_log.csv"), "PASS")
    write_audit_csv(reject_rows, os.path.join(reject_dir, "audit_log.csv"), "REJECT")
    write_audit_csv(borderline_rows, os.path.join(borderline_dir, "audit_log.csv"), "BORDERLINE")

    # Write combined audit_summary.csv
    summary_csv = os.path.join(output_dir, "audit_summary.csv")
    with open(summary_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["filename", "classification", "graphics_score", "non_graphics_score", "reasons", "audit_date"])
        writer.writeheader()
        for r in sorted(results, key=lambda x: x.get("graphics_score", 0), reverse=True):
            writer.writerow({
                "filename": r["filename"],
                "classification": r["classification"],
                "graphics_score": r.get("graphics_score", ""),
                "non_graphics_score": r.get("non_graphics_score", ""),
                "reasons": r.get("reasons", ""),
                "audit_date": audit_date,
            })
    print(f"    📝 audit_summary.csv — {len(results)} entries")

    # Write JSON summary
    summary = {
        "total": len(results),
        "pass": len(pass_list),
        "reject": len(reject_list),
        "borderline": len(borderline_list),
        "errors": len(error_list),
        "pass_pct": round(len(pass_list) / max(len(results), 1) * 100, 1),
        "reject_pct": round(len(reject_list) / max(len(results), 1) * 100, 1),
        "borderline_pct": round(len(borderline_list) / max(len(results), 1) * 100, 1),
        "audit_date": audit_date,
    }
    summary_path = os.path.join(output_dir, "audit_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"    📝 audit_summary.json")

    return summary


# resolve_drive_urls removed — live_url now comes from metadata JSON download_url


# ─── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CLIP Audit Pipeline — Classify images via HuggingFace (no local GPU needed)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/clip_audit_hf.py
  python tools/clip_audit_hf.py --input "https://drive.google.com/drive/folders/ABC123" --output "https://drive.google.com/drive/folders/XYZ789"
  python tools/clip_audit_hf.py --input "gdrive:/FIRST/part_001/files" --output "gdrive:/FIRST/audited"
  python tools/clip_audit_hf.py --input ./local_images --output ./results --pass-threshold 0.75
        """,
    )
    parser.add_argument("--input", "-i", help="Input: Google Drive URL, rclone path, or local directory")
    parser.add_argument("--output", "-o", help="Output: Google Drive URL, rclone path, or local directory")
    parser.add_argument("--pass-threshold", type=float, default=0.70,
                        help="Graphics score >= this → PASS (default: 0.70)")
    parser.add_argument("--reject-threshold", type=float, default=0.40,
                        help="Graphics score < this → REJECT (default: 0.40)")
    parser.add_argument("--batch-size", type=int, default=32,
                        help="Images per API batch (default: 32)")
    parser.add_argument("--hf-token", default=None,
                        help="HuggingFace API token (optional, raises rate limits)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify only, don't upload to Drive")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show per-image classification details")
    args = parser.parse_args()

    # Get HF token from env if not provided
    hf_token = args.hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")

    print("=" * 65)
    print("  CLIP AUDIT PIPELINE — HuggingFace Inference API")
    print("  (No local GPU or model weights needed)")
    print("=" * 65)

    # ── Step 0: Get input/output paths ──────────────────────────────
    input_path = args.input
    output_path = args.output

    if not input_path:
        print()
        input_path = input("  📂 Enter INPUT folder (Google Drive URL / rclone path / local dir): ").strip()
    if not output_path:
        output_path = input("  📁 Enter OUTPUT folder (Google Drive URL / rclone path / local dir): ").strip()

    if not input_path or not output_path:
        print("ERROR: Both input and output paths are required.")
        sys.exit(1)

    print(f"\n  Input:     {input_path}")
    print(f"  Output:    {output_path}")
    print(f"  Pass:      >={args.pass_threshold}")
    print(f"  Reject:    <{args.reject_threshold}")
    print(f"  HF Token:  {'provided' if hf_token else 'not set (using free tier)'}")
    print("=" * 65)

    # ── Step 1: Resolve input ───────────────────────────────────────
    print("\n[1/6] Resolving input path...")

    input_remote = ""
    input_rclone_path = ""
    local_input_dir = ""

    if os.path.isdir(input_path):
        # Local directory
        local_input_dir = input_path
        print(f"  ✅ Local directory: {input_path}")
    elif "drive.google.com" in input_path:
        # Google Drive URL — need to download via rclone
        folder_id, _ = parse_drive_url(input_path)
        if not folder_id:
            print(f"  ❌ Could not parse Drive URL: {input_path}")
            sys.exit(1)

        # Check if rclone has this folder accessible
        print(f"  📥 Downloading from Drive folder: {folder_id}...")
        local_input_dir = os.path.join(tempfile.gettempdir(), f"clip_audit_input_{folder_id[:8]}")
        os.makedirs(local_input_dir, exist_ok=True)

        # Try to download via rclone
        # The user may have the folder in their gdrive remote
        found = False
        for remote_name in ["gdrive", "drive", "gd"]:
            try:
                result = rclone_run(["lsjson", f"{remote_name}:/{folder_id}"], timeout=30)
                if result.returncode == 0:
                    rclone_download_folder(remote_name, f"/{folder_id}", local_input_dir)
                    input_remote = remote_name
                    input_rclone_path = f"/{folder_id}"
                    found = True
                    break
            except Exception:
                continue

        if not found:
            # Try to find by listing all remotes
            print("  ⚠️  Could not find folder in rclone remotes.")
            print("     Make sure you've run: rclone config")
            print("     And the Drive folder is accessible.")
            # Fall back to asking user for rclone path
            rclone_path = input("  Enter rclone path (e.g. gdrive:/FIRST/files): ").strip()
            if ":" in rclone_path:
                parts = rclone_path.split(":", 1)
                input_remote = parts[0]
                input_rclone_path = parts[1]
                local_input_dir = os.path.join(tempfile.gettempdir(), f"clip_audit_{int(time.time())}")
                os.makedirs(local_input_dir, exist_ok=True)
                rclone_download_folder(input_remote, input_rclone_path, local_input_dir)
            else:
                print("  ❌ Invalid rclone path")
                sys.exit(1)

        print(f"  ✅ Downloaded to: {local_input_dir}")
    elif ":" in input_path:
        # rclone path
        parts = input_path.split(":", 1)
        input_remote = parts[0]
        input_rclone_path = parts[1]
        local_input_dir = os.path.join(tempfile.gettempdir(), f"clip_audit_{int(time.time())}")
        os.makedirs(local_input_dir, exist_ok=True)
        print(f"  📥 Downloading from rclone: {input_path}...")
        rclone_download_folder(input_remote, input_rclone_path, local_input_dir)
        print(f"  ✅ Downloaded to: {local_input_dir}")
    else:
        print(f"  ❌ Could not resolve input: {input_path}")
        sys.exit(1)

    # Count images
    image_files = [f for f in os.listdir(local_input_dir)
                   if f.lower().endswith((".png", ".jpg", ".jpeg"))]
    if not image_files:
        print(f"  ❌ No images found in {local_input_dir}")
        sys.exit(1)
    print(f"  📊 Found {len(image_files)} images to classify")

    # ── Step 2: Resolve output ──────────────────────────────────────
    print("\n[2/6] Resolving output path...")

    output_remote = ""
    output_rclone_path = ""
    local_output_dir = ""

    if output_path.startswith(".") or (os.path.isdir(output_path) and ":" not in output_path):
        # Local directory (starts with . or is an existing local path without :)
        local_output_dir = output_path
        os.makedirs(local_output_dir, exist_ok=True)
        print(f"  ✅ Local output: {output_path}")
    elif "drive.google.com" in output_path:
        folder_id, _ = parse_drive_url(output_path)
        if folder_id:
            output_rclone_path = f"/{folder_id}"
            # Find the remote
            for remote_name in ["gdrive", "drive", "gd"]:
                try:
                    result = rclone_run(["lsjson", f"{remote_name}:/{folder_id}"], timeout=30)
                    if result.returncode == 0:
                        output_remote = remote_name
                        break
                except Exception:
                    continue

            if not output_remote:
                rclone_path = input("  Enter output rclone path (e.g. gdrive:/FIRST/audited): ").strip()
                if ":" in rclone_path:
                    parts = rclone_path.split(":", 1)
                    output_remote = parts[0]
                    output_rclone_path = parts[1]

            local_output_dir = os.path.join(tempfile.gettempdir(), f"clip_audit_output_{int(time.time())}")
            os.makedirs(local_output_dir, exist_ok=True)
            print(f"  ✅ Output will be uploaded to: {output_path}")
    elif ":" in output_path:
        parts = output_path.split(":", 1)
        output_remote = parts[0]
        output_rclone_path = parts[1]
        local_output_dir = os.path.join(tempfile.gettempdir(), f"clip_audit_output_{int(time.time())}")
        os.makedirs(local_output_dir, exist_ok=True)
        print(f"  ✅ Output will be uploaded to: {output_path}")
    else:
        local_output_dir = output_path
        os.makedirs(local_output_dir, exist_ok=True)
        print(f"  ✅ Local output: {output_path}")

    # ── Step 3: Classify ────────────────────────────────────────────
    print("\n[3/6] Classifying images via HuggingFace CLIP API...")

    classifier = HFClipClassifier(
        pass_threshold=args.pass_threshold,
        reject_threshold=args.reject_threshold,
        hf_token=hf_token,
        batch_size=args.batch_size,
    )

    results = classifier.classify_directory(local_input_dir)

    if not results:
        print("  ❌ No images classified")
        sys.exit(1)

    # ── Step 4: Sort and write audit files ──────────────────────────
    print("\n[4/6] Sorting images and generating audit files...")

    summary = sort_and_write(
        results, local_input_dir, local_output_dir,
        drive_remote=input_remote, drive_folder_id=input_rclone_path,
    )

    # ── Step 5: Upload to Drive ─────────────────────────────────────
    if output_remote and output_rclone_path and not args.dry_run:
        print(f"\n[5/6] Uploading results to Drive ({output_remote}:{output_rclone_path})...")

        # Create remote folders
        for category in ["PASS", "REJECT", "BORDERLINE"]:
            rclone_mkdir(output_remote, f"{output_rclone_path}/{category}")

        # Upload each category folder
        for category in ["PASS", "REJECT", "BORDERLINE"]:
            local_cat = os.path.join(local_output_dir, category)
            if os.path.isdir(local_cat) and os.listdir(local_cat):
                print(f"  📤 Uploading {category}/ ({len(os.listdir(local_cat))} files)...")
                rclone_copy_to_remote(local_cat, output_remote, f"{output_rclone_path}/{category}")

        # Upload summary files
        for fname in ["audit_summary.csv", "audit_summary.json"]:
            fpath = os.path.join(local_output_dir, fname)
            if os.path.exists(fpath):
                rclone_copy_to_remote(fpath, output_remote, output_rclone_path)

        print("  ✅ Upload complete")
    else:
        print(f"\n[5/6] Skipping Drive upload (dry-run or local output)")

    # ── Final Report ────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  AUDIT COMPLETE")
    print("=" * 65)
    print(f"  Total:      {summary['total']}")
    print(f"  PASS:       {summary['pass']} ({summary['pass_pct']}%)")
    print(f"  BORDERLINE: {summary['borderline']} ({summary['borderline_pct']}%)")
    print(f"  REJECT:     {summary['reject']} ({summary['reject_pct']}%)")
    print("=" * 65)
    print(f"\n  📁 Output: {local_output_dir}/")
    print(f"     PASS/         — {summary['pass']} images + audit_log.csv")
    print(f"     REJECT/       — {summary['reject']} images + audit_log.csv")
    print(f"     BORDERLINE/   — {summary['borderline']} images + audit_log.csv")
    print(f"     audit_summary.csv")
    print(f"     audit_summary.json")
    if output_remote:
        print(f"\n  📤 Uploaded to: {output_remote}:{output_rclone_path}")
    print("=" * 65)


if __name__ == "__main__":
    main()
