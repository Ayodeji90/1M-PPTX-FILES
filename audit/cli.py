#!/usr/bin/env python3
"""
Audit CLI — Classify images in a batch folder as graphics-heavy or non-qualifying.

Usage:
    python -m audit.cli <batch_dir> [--output report.csv] [--pass-threshold 70] [--reject-threshold 40]
    python -m audit.cli batch_001/part_001/files/ --output audit_report.csv
    python -m audit.cli batch_001/part_001/files/ --review-dir /tmp/review/
"""
import argparse
import csv
import json
import os
import sys
import shutil
from pathlib import Path

from .classifier import extract_features, classify


CSV_HEADERS = [
    "filename",
    "classification",
    "score",
    "text_density",
    "line_count",
    "perpendicular_pairs",
    "distinct_colors",
    "color_entropy",
    "edge_density",
    "long_edge_ratio",
    "medium_blobs",
    "symmetry",
    "white_background",
    "hue_diversity",
    "rejection_reason",
]


def audit_batch(batch_dir: str, output_csv: str = None, pass_threshold: int = 70,
                reject_threshold: int = 40, review_dir: str = None, verbose: bool = False):
    """Audit all PNG images in a batch directory."""
    batch_path = Path(batch_dir)
    if not batch_path.exists():
        print(f"Error: Directory not found: {batch_dir}")
        sys.exit(1)
    
    # Find all PNG files
    png_files = sorted(batch_path.glob("*.png"))
    if not png_files:
        print(f"Error: No PNG files found in {batch_dir}")
        sys.exit(1)
    
    print(f"Auditing {len(png_files)} images in {batch_dir}...")
    print(f"Pass threshold: ≥{pass_threshold}, Reject threshold: <{reject_threshold}")
    print()
    
    # Create review directory if specified
    if review_dir:
        review_path = Path(review_dir)
        review_path.mkdir(parents=True, exist_ok=True)
    
    results = []
    pass_count = 0
    borderline_count = 0
    reject_count = 0
    
    for i, png_file in enumerate(png_files, 1):
        if verbose:
            print(f"  [{i}/{len(png_files)}] {png_file.name}...", end=" ", flush=True)
        
        features = extract_features(str(png_file))
        
        # Re-classify with custom thresholds
        features.classification = classify(features.score, pass_threshold, reject_threshold)
        
        # Add rejection reason
        if features.classification == "reject":
            if features.text_density > 0.4:
                features.rejection_reason = "text_heavy"
            elif features.text_density > 0.2:
                features.rejection_reason = "moderate_text"
            elif features.distinct_colors <= 2:
                features.rejection_reason = "low_color_diversity"
            elif features.perpendicular_pairs == 0 and features.line_count < 5:
                features.rejection_reason = "no_chart_structure"
            else:
                features.rejection_reason = "low_score"
        elif features.classification == "borderline":
            features.rejection_reason = "needs_review"
        
        # Move borderline to review dir
        if review_dir and features.classification == "borderline":
            dest = Path(review_dir) / png_file.name
            shutil.copy2(str(png_file), str(dest))
        
        # Count
        if features.classification == "pass":
            pass_count += 1
        elif features.classification == "borderline":
            borderline_count += 1
        else:
            reject_count += 1
        
        if verbose:
            print(f"{features.classification} (score={features.score})")
        
        results.append(features)
    
    # Print summary
    total = len(results)
    print()
    print("=" * 60)
    print("AUDIT RESULTS")
    print("=" * 60)
    print(f"Total images:   {total}")
    print(f"PASS:           {pass_count} ({pass_count * 100 // total}%)")
    print(f"BORDERLINE:     {borderline_count} ({borderline_count * 100 // total}%)")
    print(f"REJECT:         {reject_count} ({reject_count * 100 // total}%)")
    print()
    
    # Score distribution
    scores = [r.score for r in results]
    print("SCORE DISTRIBUTION:")
    bins = [0] * 10
    for s in scores:
        b = min(s // 10, 9)
        bins[b] += 1
    for i, count in enumerate(bins):
        bar = "█" * (count * 40 // max(max(bins), 1))
        print(f"  {i * 10:3d}-{(i + 1) * 10 - 1:3d}: {count:4d} {bar}")
    print()
    
    # Show rejected images
    rejected = [r for r in results if r.classification == "reject"]
    if rejected:
        print(f"REJECTED IMAGES ({len(rejected)}):")
        for r in sorted(rejected, key=lambda x: x.score)[:20]:
            print(f"  {r.filename}: score={r.score} reason={r.rejection_reason} text={r.text_density:.3f}")
        if len(rejected) > 20:
            print(f"  ... and {len(rejected) - 20} more")
        print()
    
    # Show borderline
    borderline = [r for r in results if r.classification == "borderline"]
    if borderline:
        print(f"BORDERLINE IMAGES ({len(borderline)}) — moved to {review_dir or 'not moved'}:")
        for r in sorted(borderline, key=lambda x: x.score, reverse=True):
            print(f"  {r.filename}: score={r.score}")
        print()
    
    # Write CSV report
    if output_csv:
        csv_path = Path(output_csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)
            for r in results:
                writer.writerow([
                    r.filename, r.classification, r.score,
                    r.text_density, r.line_count, r.perpendicular_pairs,
                    r.distinct_colors, r.color_entropy, r.edge_density,
                    r.long_edge_ratio, r.medium_blobs, r.symmetry,
                    r.white_background, r.hue_diversity, r.rejection_reason,
                ])
        print(f"Report saved to: {csv_path}")
    
    # Write JSON summary
    summary = {
        "batch_dir": str(batch_dir),
        "total": total,
        "pass": pass_count,
        "borderline": borderline_count,
        "reject": reject_count,
        "pass_rate": round(pass_count / total * 100, 1),
        "avg_score": round(sum(scores) / len(scores), 1),
        "min_score": min(scores),
        "max_score": max(scores),
    }
    summary_path = Path(output_csv or "audit_report.csv").with_suffix(".json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary saved to: {summary_path}")
    
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Audit images in a batch folder for graphics-heavy content",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m audit.cli batch_001/part_001/files/
    python -m audit.cli batch_001/part_001/files/ --output report.csv
    python -m audit.cli batch_001/part_001/files/ --pass-threshold 80 --reject-threshold 50
    python -m audit.cli batch_001/part_001/files/ --review-dir /tmp/review/ --verbose
        """,
    )
    parser.add_argument("batch_dir", help="Directory containing PNG images to audit")
    parser.add_argument("--output", "-o", help="Output CSV report path", default="audit_report.csv")
    parser.add_argument("--pass-threshold", type=int, default=70, help="Minimum score to pass (default: 70)")
    parser.add_argument("--reject-threshold", type=int, default=40, help="Score below which to reject (default: 40)")
    parser.add_argument("--review-dir", help="Directory to move borderline images to")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print per-image results")
    
    args = parser.parse_args()
    audit_batch(
        args.batch_dir,
        output_csv=args.output,
        pass_threshold=args.pass_threshold,
        reject_threshold=args.reject_threshold,
        review_dir=args.review_dir,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
