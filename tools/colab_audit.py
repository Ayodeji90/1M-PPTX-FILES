#!/usr/bin/env python3
"""
CLIP Zero-Shot Image Auditor — CLI Version

Classifies images as graphics-heavy (charts, graphs, maps) or non-qualifying
(text, photos, decorative). Sorts into PASS/REJECT/BORDERLINE folders.

Usage:
    # On Colab GPU (via colab CLI):
    colab run --gpu T4 tools/colab_audit.py --input /content/drive/MyDrive/batch_001 --output /content/drive/MyDrive/audited

    # Locally:
    python tools/colab_audit.py --input ./batch_001/part_001/files --output ./audited

    # With custom thresholds:
    python tools/colab_audit.py --input ./images --output ./results --pass-threshold 0.75 --reject-threshold 0.35
"""

import argparse
import csv
import json
import os
import shutil
import sys
import time
from pathlib import Path

import torch
import clip
from PIL import Image


# ── Classification Prompts ──────────────────────────────────────────────

GRAPHICS_PROMPTS = [
    "a chart with axes and data points",
    "a bar chart showing data",
    "a pie chart with colored segments",
    "a line graph with trends",
    "a map with geographic data",
    "an infographic with visual data",
    "a diagram showing a process or flow",
    "a scatter plot with data distribution",
    "a data visualization dashboard",
    "a technical drawing or schematic",
    "a heatmap showing data patterns",
    "a funnel chart or waterfall chart",
    "a radar chart comparing multiple variables",
    "a stacked bar chart or grouped bar chart",
]

NON_GRAPHICS_PROMPTS = [
    "a slide with bullet points and text",
    "a photograph of people or scenery",
    "a title slide with large text",
    "a table with rows and columns of text",
    "a page with mostly text content",
    "a decorative image or logo",
    "a closing slide with contact information",
    "a photo of a presentation speaker",
    "a slide with quotes or testimonials",
    "a slide with company branding",
    "a white background with small text",
    "a screenshot of a software interface",
    "a clip art or stock illustration",
    "a handwritten note or whiteboard",
]


# ── Classifier ──────────────────────────────────────────────────────────

class CLIPClassifier:
    """Batch CLIP classifier for graphics-heavy image detection."""

    def __init__(self, pass_threshold=0.70, reject_threshold=0.40, batch_size=64):
        self.pass_threshold = pass_threshold
        self.reject_threshold = reject_threshold
        self.batch_size = batch_size

        # Load model
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"Loading CLIP ViT-B/32 on {self.device}...")
        self.model, self.preprocess = clip.load("ViT-B/32", device=self.device)

        # Encode text prompts
        graphics_tokens = clip.tokenize(GRAPHICS_PROMPTS).to(self.device)
        non_graphics_tokens = clip.tokenize(NON_GRAPHICS_PROMPTS).to(self.device)

        with torch.no_grad():
            self.graphics_features = self.model.encode_text(graphics_tokens).mean(dim=0)
            self.non_graphics_features = self.model.encode_text(non_graphics_tokens).mean(dim=0)
            self.graphics_features = self.graphics_features / self.graphics_features.norm()
            self.non_graphics_features = self.non_graphics_features / self.non_graphics_features.norm()

        print(f"Ready: {len(GRAPHICS_PROMPTS)} graphics + {len(NON_GRAPHICS_PROMPTS)} non-graphics prompts encoded")

    def classify_batch(self, image_paths):
        """Classify a batch of images. Returns list of result dicts."""
        results = []

        try:
            images = []
            valid_paths = []
            for p in image_paths:
                try:
                    img = Image.open(p).convert("RGB")
                    images.append(self.preprocess(img))
                    valid_paths.append(p)
                except Exception as e:
                    results.append({
                        "filename": os.path.basename(p),
                        "graphics_score": 0.0,
                        "non_graphics_score": 0.0,
                        "classification": "error",
                        "reasons": str(e),
                    })

            if not images:
                return results

            batch = torch.stack(images).to(self.device)

            with torch.no_grad():
                image_features = self.model.encode_image(batch)
                image_features = image_features / image_features.norm(dim=-1, keepdim=True)

                graphics_scores = (image_features @ self.graphics_features).cpu()
                non_graphics_scores = (image_features @ self.non_graphics_features).cpu()

                probs = torch.softmax(
                    torch.stack([graphics_scores, non_graphics_scores], dim=-1) * 100,
                    dim=-1,
                )

            for i, path in enumerate(valid_paths):
                graphics_prob = probs[i, 0].item()
                non_graphics_prob = probs[i, 1].item()

                reasons = []
                if graphics_prob > self.pass_threshold:
                    classification = "pass"
                    if graphics_prob > 0.90:
                        reasons.append("high_confidence_graphics")
                    else:
                        reasons.append("moderate_confidence_graphics")
                elif graphics_prob > self.reject_threshold:
                    classification = "borderline"
                    if graphics_prob > 0.55:
                        reasons.append("leaning_graphics_but_uncertain")
                    elif graphics_prob > 0.45:
                        reasons.append("even_split_graphics_vs_non")
                    else:
                        reasons.append("leaning_non_graphics_but_uncertain")
                else:
                    classification = "reject"
                    if graphics_prob < 0.20:
                        reasons.append("clearly_non_graphics")
                    else:
                        reasons.append("likely_non_graphics")

                results.append({
                    "filename": os.path.basename(path),
                    "graphics_score": round(graphics_prob, 4),
                    "non_graphics_score": round(non_graphics_prob, 4),
                    "classification": classification,
                    "reasons": "; ".join(reasons),
                })

        except Exception as e:
            for p in image_paths:
                results.append({
                    "filename": os.path.basename(p),
                    "graphics_score": 0.0,
                    "non_graphics_score": 0.0,
                    "classification": "error",
                    "reasons": str(e),
                })

        return results

    def classify_images(self, image_dir):
        """Classify all PNGs in a directory using batched processing."""
        png_files = sorted([
            f for f in os.listdir(image_dir)
            if f.lower().endswith(".png")
        ])

        if not png_files:
            print(f"No PNG files found in {image_dir}")
            return []

        print(f"\nClassifying {len(png_files)} images (batch_size={self.batch_size})...\n")

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

            # Progress update every batch
            batch_pass = sum(1 for r in batch_results if r["classification"] == "pass")
            batch_reject = sum(1 for r in batch_results if r["classification"] == "reject")
            batch_border = sum(1 for r in batch_results if r["classification"] == "borderline")

            print(
                f"  [{done:>5}/{len(png_files)}] "
                f"pass={batch_pass} reject={batch_reject} borderline={batch_border} | "
                f"{rate:.1f} img/sec | ETA: {eta:.0f}s"
            )

        total_time = time.time() - start_time
        print(f"\nDone: {len(all_results)} images in {total_time:.1f}s ({len(all_results)/total_time:.1f} img/sec)")

        return all_results


# ── Output Sorting ───────────────────────────────────────────────────────

def sort_and_write(results, input_dir, output_dir):
    """Sort images into PASS/REJECT/BORDERLINE folders with audit CSVs."""
    pass_dir = os.path.join(output_dir, "PASS")
    reject_dir = os.path.join(output_dir, "REJECT")
    borderline_dir = os.path.join(output_dir, "BORDERLINE")

    for d in [pass_dir, reject_dir, borderline_dir]:
        os.makedirs(d, exist_ok=True)

    pass_list = [r for r in results if r["classification"] == "pass"]
    reject_list = [r for r in results if r["classification"] == "reject"]
    borderline_list = [r for r in results if r["classification"] == "borderline"]
    error_list = [r for r in results if r["classification"] == "error"]

    # Copy images to folders
    def copy_images(file_list, dest_dir):
        copied = 0
        for r in file_list:
            src = os.path.join(input_dir, r["filename"])
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dest_dir, r["filename"]))
                copied += 1
        return copied

    print("\nSorting images into folders...")
    pass_count = copy_images(pass_list, pass_dir)
    reject_count = copy_images(reject_list, reject_dir)
    border_count = copy_images(borderline_list, borderline_dir)

    print(f"  PASS:      {pass_count} images → {pass_dir}")
    print(f"  REJECT:    {reject_count} images → {reject_dir}")
    print(f"  BORDERLINE: {border_count} images → {borderline_dir}")
    if error_list:
        print(f"  ERRORS:    {len(error_list)} (could not classify)")

    # Write audit CSVs
    def write_csv(file_list, csv_path):
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["filename", "classification", "graphics_score", "non_graphics_score", "reasons"])
            for r in sorted(file_list, key=lambda x: x.get("graphics_score", 0), reverse=True):
                writer.writerow([
                    r["filename"],
                    r["classification"],
                    r["graphics_score"],
                    r["non_graphics_score"],
                    r["reasons"],
                ])

    write_csv(pass_list, os.path.join(pass_dir, "audit_log.csv"))
    write_csv(reject_list, os.path.join(reject_dir, "audit_log.csv"))
    write_csv(borderline_list, os.path.join(borderline_dir, "audit_log.csv"))

    # Summary CSV
    write_csv(results, os.path.join(output_dir, "audit_summary.csv"))

    # JSON summary
    summary = {
        "total": len(results),
        "pass": len(pass_list),
        "reject": len(reject_list),
        "borderline": len(borderline_list),
        "errors": len(error_list),
        "pass_pct": round(len(pass_list) / max(len(results), 1) * 100, 1),
        "reject_pct": round(len(reject_list) / max(len(results), 1) * 100, 1),
        "borderline_pct": round(len(borderline_list) / max(len(results), 1) * 100, 1),
    }
    with open(os.path.join(output_dir, "audit_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    return summary


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CLIP Zero-Shot Image Auditor")
    parser.add_argument("--input", "-i", required=True, help="Input directory with PNG images")
    parser.add_argument("--output", "-o", required=True, help="Output directory for sorted results")
    parser.add_argument("--pass-threshold", type=float, default=0.70, help="Graphics score threshold to PASS (default: 0.70)")
    parser.add_argument("--reject-threshold", type=float, default=0.40, help="Graphics score threshold below which to REJECT (default: 0.40)")
    parser.add_argument("--batch-size", type=int, default=64, help="Batch size for GPU inference (default: 64)")
    args = parser.parse_args()

    input_dir = args.input
    output_dir = args.output

    if not os.path.isdir(input_dir):
        print(f"Error: Input directory not found: {input_dir}")
        sys.exit(1)

    print("=" * 60)
    print("  CLIP ZERO-SHOT IMAGE AUDITOR")
    print("=" * 60)
    print(f"  Input:   {input_dir}")
    print(f"  Output:  {output_dir}")
    print(f"  Pass:    >{args.pass_threshold}")
    print(f"  Reject:  <{args.reject_threshold}")
    print(f"  Batch:   {args.batch_size}")
    print("=" * 60)

    # Classify
    classifier = CLIPClassifier(
        pass_threshold=args.pass_threshold,
        reject_threshold=args.reject_threshold,
        batch_size=args.batch_size,
    )
    results = classifier.classify_images(input_dir)

    if not results:
        print("No images to classify.")
        sys.exit(0)

    # Sort and write
    summary = sort_and_write(results, input_dir, output_dir)

    # Final report
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)
    print(f"  Total:      {summary['total']}")
    print(f"  PASS:       {summary['pass']} ({summary['pass_pct']}%)")
    print(f"  BORDERLINE: {summary['borderline']} ({summary['borderline_pct']}%)")
    print(f"  REJECT:     {summary['reject']} ({summary['reject_pct']}%)")
    print("=" * 60)
    print(f"\n  Output: {output_dir}/")
    print(f"    PASS/         — {summary['pass']} images + audit_log.csv")
    print(f"    REJECT/       — {summary['reject']} images + audit_log.csv")
    print(f"    BORDERLINE/   — {summary['borderline']} images + audit_log.csv")
    print(f"    audit_summary.csv")
    print(f"    audit_summary.json")


if __name__ == "__main__":
    main()
