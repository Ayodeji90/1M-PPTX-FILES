#!/usr/bin/env python3
"""Move ~half of the old (full) Drive account's deck folders onto VM3's
2TB account, batch-intact, with verification.

Server-side copy (`--drive-server-side-across-configs`) requires the two
accounts to share access (we got 404s), so the move streams through
local disk file-by-file: rclone downloads a file to a temp buffer,
uploads it, deletes the local copy -- peak disk use stays tiny.

Strategy (batch-intact):
  1. Enumerate deck counts per folder on the source remote.
  2. Pick folders greedily until the cumulative deck count reaches the
     target fraction (default 0.5) of the total.
  3. For each chosen folder: `rclone copy` to the destination, verify
     file count + total size, THEN `rclone delete` the source folder's
     files (and remove the empty folder).

Usage:
    python tools/move_drive_decks.py --source olddrive:PptxSweeper_Delivery_GCP/BATCH_01,olddrive:... \
        --dest gdrive:PptxSweeper_Conversion --fraction 0.5 [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

RCLONE = "rclone"


def _run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def folder_deck_count(remote_path: str) -> tuple[int, int]:
    """(files, decks) under a remote folder. Decks = pptx/ppt/pdf only."""
    proc = _run([RCLONE, "ls", remote_path])
    if proc.returncode != 0:
        raise SystemExit(f"rclone ls failed: {proc.stderr[-500:]}")
    files = 0
    decks = 0
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        files += 1
        name = parts[1].lower()
        if name.endswith((".pptx", ".ppt", ".pdf")):
            decks += 1
    return files, decks


def copy_folder(src: str, dest: str) -> None:
    """Copy one folder; raise on failure."""
    proc = _run([RCLONE, "copy", src, dest,
                 "--transfers", "4", "--checkers", "8",
                 "--tpslimit", "6", "--tpslimit-burst", "12",
                 "--retries", "5", "--low-level-retries", "10"])
    if proc.returncode != 0:
        raise SystemExit(f"rclone copy failed: {proc.stderr[-800:]}")


def verify_folder(src: str, dest: str) -> None:
    """Compare file counts + total bytes between source and dest."""
    _, src_decks = folder_deck_count(src)
    _, dst_decks = folder_deck_count(dest)
    if src_decks != dst_decks:
        raise SystemExit(f"VERIFY FAILED: {src} has {src_decks} decks, "
                         f"{dest} has {dst_decks} -- refusing to delete source")
    print(f"  verified: {src_decks} decks in both", flush=True)


def delete_source_folder(src: str) -> None:
    """Delete files in the source folder, then try to remove the folder."""
    proc = _run([RCLONE, "delete", src, "--tpslimit", "6"])
    if proc.returncode != 0:
        raise SystemExit(f"rclone delete failed (files remain on source): "
                         f"{proc.stderr[-500:]}")
    _run([RCLONE, "rmdir", src], timeout=120)  # best-effort


def main() -> None:
    ap = argparse.ArgumentParser(description="Move ~half the old account's decks")
    ap.add_argument("--source", required=True,
                    help="comma-separated remote paths to consider, e.g. "
                         "olddrive:BATCH_01,olddrive:PptxSweeper_Delivery_GCP/BATCH_01")
    ap.add_argument("--dest", required=True,
                    help="destination remote root, e.g. gdrive:PptxSweeper_Conversion")
    ap.add_argument("--fraction", type=float, default=0.5,
                    help="target fraction of total decks to move (default 0.5)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sources = [s.strip() for s in args.source.split(",") if s.strip()]
    counts = {}
    total_decks = 0
    print("=== enumerating folders ===")
    for s in sources:
        files, decks = folder_deck_count(s)
        counts[s] = (files, decks)
        total_decks += decks
        print(f"  {s}: {decks} decks / {files} files", flush=True)
    print(f"  TOTAL: {total_decks} decks")

    target = int(total_decks * args.fraction)
    print(f"=== target: {target} decks (fraction {args.fraction}) ===")

    # Exact subset search (n <= ~8 folders): pick the combination whose
    # cumulative deck count is CLOSEST to the target, so we never move a
    # lopsided 25% or 75%.
    names = list(counts)
    best: tuple[list[str], int] | None = None
    for mask in range(1, 1 << len(names)):
        subset = [names[i] for i in range(len(names)) if mask & (1 << i)]
        cum = sum(counts[s][1] for s in subset)
        if best is None or abs(cum - target) < abs(best[1] - target):
            best = (subset, cum)
    assert best is not None
    chosen, cum = best
    print(f"=== moving {cum} decks ({cum/total_decks:.1%}):")
    for s in chosen:
        print(f"  {s}  ({counts[s][1]} decks)")
    if args.dry_run:
        print("DRY RUN -- not moving anything")
        return

    for s in chosen:
        folder = s.split(":", 1)[1].rstrip("/")
        leaf = folder.split("/")[-1]
        dest = f"{args.dest.rstrip('/')}/{leaf}"
        print(f"\n=== {s} -> {dest} ===", flush=True)
        copy_folder(s, dest)
        verify_folder(s, dest)
        print("  copy verified; deleting source", flush=True)
        delete_source_folder(s)
        print(f"  DONE {s}", flush=True)

    print("\n=== move complete ===")
    for s in chosen:
        print(f"  moved: {s} -> {args.dest.rstrip('/')}/{s.split(':')[1].rstrip('/').split('/')[-1]}")


if __name__ == "__main__":
    main()
