#!/usr/bin/env python3
"""Inventory or copy image-delivery folders from rclone to Amazon S3.

The default action is a read-only inventory. Pass --copy to transfer files.
Pass --delete-source only after the verified copy is complete; this option
deletes source files, so it requires --copy as well.

Example:
  python tools/migrate_drive_images_to_s3.py \
    --source gdrive:PptxSweeper_Image_Delivery_VM1 \
    --dest aws:my-bucket/pptx-images --copy
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from decimal import Decimal


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def listing(rclone: str, path: str) -> list[dict]:
    p = run([rclone, "lsjson", "-R", "--hash", path])
    if p.returncode:
        raise RuntimeError(f"Cannot list {path}: {p.stderr[-1200:]}")
    data = json.loads(p.stdout or "[]")
    return [e for e in data if not e.get("IsDir")]


def unique_files(files: list[dict], path: str) -> list[dict]:
    """Collapse only byte-identical Drive duplicate-name entries.

    Google Drive can contain multiple files with the same name in a folder.
    rclone's check compares the unique path set, so only collapse duplicates
    when size and at least one matching content hash prove they are identical.
    """
    grouped: dict[str, list[dict]] = {}
    for entry in files:
        grouped.setdefault(entry["Path"], []).append(entry)
    result = []
    for name, entries in grouped.items():
        if len(entries) == 1:
            result.append(entries[0])
            continue
        first = entries[0]
        first_hashes = first.get("Hashes") or {}
        if not first_hashes or any(
            int(entry.get("Size", 0)) != int(first.get("Size", 0))
            or not (set(first_hashes) & set(entry.get("Hashes") or {}))
            or any(first_hashes[k] != entry["Hashes"][k]
                   for k in set(first_hashes) & set(entry.get("Hashes") or {}))
            for entry in entries[1:]
        ):
            raise RuntimeError(f"Ambiguous duplicate filename in {path}: {name}")
        print(f"Identical duplicate entries: {len(entries)} copies of {name}")
        result.append(first)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="rclone Drive path, e.g. gdrive:ImageRoot")
    ap.add_argument("--dest", required=True, help="rclone S3 path, e.g. aws:bucket/prefix")
    ap.add_argument("--rclone", default="rclone")
    ap.add_argument("--copy", action="store_true", help="perform the copy (default is inventory only)")
    ap.add_argument("--delete-source", action="store_true", help="delete Drive files only after copy+verify")
    ap.add_argument("--confirm-source", help="exact source path confirmation for scripted deletion")
    ap.add_argument("--verify-download", action="store_true",
                    help="verify by reading object contents after transfer (extra transfer time/cost)")
    ap.add_argument("--storage-rate", default="0.023", type=Decimal,
                    help="USD per GB-month; set to the selected region's S3 Standard rate")
    ap.add_argument("--months", type=int, choices=(2, 3), default=3)
    args = ap.parse_args()

    if args.delete_source and not args.copy:
        ap.error("--delete-source requires --copy")
    if args.delete_source and not args.verify_download:
        ap.error("--delete-source requires --verify-download for content-level verification")

    try:
        files = listing(args.rclone, args.source)
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        files = unique_files(files, args.source)
    except RuntimeError as exc:
        print(f"ERROR: {exc}; source retained.", file=sys.stderr)
        return 3

    total = sum(int(f.get("Size", 0)) for f in files)
    gb = Decimal(total) / Decimal(1_000_000_000)
    print(f"Source: {args.source}\nFiles: {len(files):,}\nBytes: {total:,} ({gb:.3f} GB)")
    print(f"S3 Standard storage estimate: ${gb * args.storage_rate:.2f}/month; "
          f"${gb * args.storage_rate * args.months:.2f}/{args.months} months")
    print("Estimate excludes request charges, taxes, retrieval/egress, and the temporary VM.")
    if not args.copy:
        print("Inventory only. Review the source and estimate, then rerun with --copy.")
        return 0

    copy = run([args.rclone, "copy", args.source, args.dest,
                "--transfers", "4", "--checkers", "8", "--retries", "8",
                "--low-level-retries", "20", "--progress"])
    if copy.returncode:
        print(f"COPY FAILED; source retained. {copy.stderr[-1500:]}", file=sys.stderr)
        return copy.returncode

    verify_flags = ["--download"] if args.verify_download else ["--size-only"]
    verify = run([args.rclone, "check", args.source, args.dest, *verify_flags])
    if verify.returncode:
        print(f"VERIFY FAILED; source retained. {verify.stderr[-1500:]}", file=sys.stderr)
        return verify.returncode

    destination = listing(args.rclone, args.dest)
    if len(destination) != len(files) or sum(int(f.get("Size", 0)) for f in destination) != total:
        print("VERIFY FAILED: destination file count/bytes differ; source retained.", file=sys.stderr)
        return 3
    print(f"Verified {len(files):,} files / {total:,} bytes at {args.dest}.")

    if args.delete_source:
        confirm = args.confirm_source
        if confirm is None:
            confirm = input(f"Type DELETE {args.source} to remove source files: ")
        if confirm != f"DELETE {args.source}":
            print("Source deletion cancelled; verified S3 copy retained.")
            return 4
        # Drive trash still consumes Google storage quota, so permanently
        # delete only after the verified copy and exact-path confirmation.
        deleted = run([args.rclone, "delete", "--drive-use-trash=false", args.source])
        if deleted.returncode:
            print(f"Source deletion failed; S3 copy remains. {deleted.stderr[-1200:]}", file=sys.stderr)
            return deleted.returncode
        print("Source files deleted after verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
