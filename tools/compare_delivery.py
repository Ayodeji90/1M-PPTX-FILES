#!/usr/bin/env python3
"""Compare Google Drive delivery folders by content (sha256 sidecars).

Use case: you have run the pipeline from several machines, each
delivering into its own Drive root (e.g. PptxSweeper_Delivery_GCP,
PptxSweeper_Delivery_VM2). Because every machine numbers batches from
BATCH_01_file_00001, the same *filename* in two folders is usually a
DIFFERENT file -- only the content hash (sha256, stored in each file's
.metadata.json sidecar) is comparable across machines.

What it does:
  1. For each remote folder, lists *.metadata.json via `rclone lsjson`.
  2. Downloads the sidecars (small) to a local temp dir.
  3. Compares sha256 sets -> exact duplicates + name collisions.
  4. Writes:
       duplicates.csv  - one row per content-identical duplicate pair
       catalog.csv     - ALL distinct sha256 (unique across both folders),
                         importable on the VM with:
                           pptxsweeper import-catalog catalog.csv
                         so those ~17k files are never re-downloaded.
  5. Prints a summary.  Usage:
  python tools/compare_delivery.py \
      gdrive:PptxSweeper_Delivery_GCP/BATCH_01 \
      gdrive:PptxSweeper_Delivery_VM2/BATCH_01 \
      [gdrive:BATCH_01 ...] \
      --out ./compare_out
      [--rclone-bin rclone]

Any number of folders may be given; the FIRST folder is the reference
"A" and every other folder is compared against it (one duplicates CSV
per pair). Put the folder whose duplicate report you care about most
first. The global catalog.csv (all distinct sha256 across every folder)
is always complete, so dedup value does not depend on ordering. Works
on the laptop or the VM -- anywhere rclone has the remote configured.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pptxsweeper.delivery_compare import (  # noqa: E402
    compare_folders, load_records, sidecar_reader,
    summarize, write_duplicates_csv,
)


def _run(cmd: list[str], timeout: int = 7200) -> str:
    """Run rclone. Default timeout is generous: bulk sidecar downloads of
    ~17k small files are Google-Drive rate-limited to ~200/min, which can
    take well over an hour for the big folders."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
                           f"{proc.stderr[-2000:]}")
    return proc.stdout


def _list_sidecars(rclone: str, remote_path: str, timeout: int) -> list[str]:
    """Names of *.metadata.json files directly under remote_path."""
    proc = subprocess.run([rclone, "lsjson", remote_path], capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        # folder missing is fine (a machine that never delivered)
        if "directory not found" in (proc.stderr or "").lower():
            return []
        raise RuntimeError(f"lsjson failed for {remote_path}: {proc.stderr[-1000:]}")
    try:
        entries = json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    return [e["Name"] for e in entries
            if e.get("IsDir") is False and e.get("Name", "").endswith(".metadata.json")]


def _fetch_sidecars(rclone: str, remote_path: str, names: list[str],
                    dest_dir: Path, timeout: int) -> int:
    """Bulk-download the named sidecars via rclone copy (fast, parallel)."""
    if not names:
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    # `--files-from` reuses the lsjson result, so rclone does not have to
    # enumerate the (much larger) folder again; small files are the Drive
    # API rate limit, not rclone, so more transfers/checkers help a lot.
    list_file = dest_dir / ".filelist.txt"
    list_file.write_text("\n".join(names), encoding="utf-8")
    _run([rclone, "copy", remote_path, str(dest_dir),
          "--files-from", str(list_file),
          "--transfers", "48", "--checkers", "96"],
         timeout=timeout)
    kept = 0
    for name in names:
        if (dest_dir / name).exists():
            kept += 1
    return kept


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", help="Remote paths, e.g. gdrive:...")
    ap.add_argument("--out", default="./compare_out", help="Output directory")
    ap.add_argument("--rclone-bin", default="rclone")
    ap.add_argument("--timeout", type=int, default=7200,
                    help="Seconds before an rclone subprocess is killed "
                         "(default 7200; big sidecar pulls are Drive-rate-limited)")
    args = ap.parse_args(argv)

    folders = args.folders
    if len(folders) < 2:
        ap.error("need at least two remote folders to compare")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_records = []
    with tempfile.TemporaryDirectory(prefix="cmp_delivery_") as tmp:
        tmp_dir = Path(tmp)
        for i, folder in enumerate(folders):
            print(f"[{i + 1}/{len(folders)}] {folder}", flush=True)
            sidecars = _list_sidecars(args.rclone_bin, folder, args.timeout)
            print(f"  sidecars: {len(sidecars)}", flush=True)
            if not sidecars:
                all_records.append((folder, []))
                continue
            local_dir = tmp_dir / f"folder_{i}"
            fetched = _fetch_sidecars(args.rclone_bin, folder, sidecars,
                                      local_dir, args.timeout)
            print(f"  downloaded: {fetched}", flush=True)
            records = load_records(sidecar_reader(local_dir))
            print(f"  parsed records: {len(records)}", flush=True)
            all_records.append((folder, records))

    # pairwise comparisons: first folder is the reference "A"
    summary_rows = []
    for i in range(1, len(all_records)):
        name_a, recs_a = all_records[0]
        name_b, recs_b = all_records[i]
        result = compare_folders(name_a, name_b, recs_a, recs_b)
        tag = f"{name_a.split(':')[-1].replace('/', '_')}__vs__{name_b.split(':')[-1].replace('/', '_')}"
        write_duplicates_csv(result, out_dir / f"duplicates_{tag}.csv")
        s = summarize(result)
        summary_rows.append(s)
        print(f"\n== {name_b} vs {name_a} ==")
        for k, v in s.items():
            print(f"  {k}: {v}")

    # one combined catalog across ALL folders (dedup on sha256)
    all_sha = sorted({r.sha256 for _, recs in all_records for r in recs})
    catalog_path = out_dir / "catalog.csv"
    with open(catalog_path, "w", newline="", encoding="utf-8") as fh:
        fh.write("sha256\n")
        for sha in all_sha:
            fh.write(f"{sha}\n")
    print(f"\nwrote: {catalog_path} ({len(all_sha)} unique sha256)")
    print("import on the VM with:  pptxsweeper import-catalog "
          f"{catalog_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
