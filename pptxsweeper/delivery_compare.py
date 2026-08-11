"""Compare delivery folders by CONTENT (sha256 from metadata sidecars).

Pure logic -- no rclone, no registry -- so it is unit-testable and the
CLI tool (tools/compare_delivery.py) and any future command can share it.

Why sha256 and not filenames: every machine numbers its own batches
starting at BATCH_01_file_00001, so the same *name* in two folders is
usually a DIFFERENT file. The only trustworthy key across machines is
the content hash, which the pipeline writes into each delivered file's
`.metadata.json` sidecar.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class FileRecord:
    sha256: str
    delivered_filename: str
    source_url: str = ""
    size: int = 0


def parse_sidecar(data: dict) -> FileRecord | None:
    """Extract {sha256, delivered_filename, source_url} from a sidecar dict.

    Returns None for sidecars missing either key (defensive: a truncated
    or foreign sidecar must not crash the whole comparison).
    """
    sha = (data.get("sha256") or "").strip().lower()
    name = (data.get("delivered_filename") or "").strip()
    if not sha or not name:
        return None
    size = data.get("size") or data.get("content_length") or 0
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    return FileRecord(sha256=sha, delivered_filename=name,
                      source_url=(data.get("source_url") or "").strip(),
                      size=size)


def load_records(sidecars: list[dict]) -> list[FileRecord]:
    """Map raw parsed-JSON sidecars to FileRecords, dropping unusable ones."""
    records = []
    for data in sidecars:
        rec = parse_sidecar(data)
        if rec is not None:
            records.append(rec)
    return records


@dataclass
class CompareResult:
    folder_a: str
    folder_b: str
    a_records: list[FileRecord] = field(default_factory=list)
    b_records: list[FileRecord] = field(default_factory=list)
    # same sha256 present in BOTH folders -> content-identical files
    duplicates: list[tuple[FileRecord, FileRecord]] = field(default_factory=list)
    # same delivered_filename in both folders but DIFFERENT sha256 ->
    # a client-facing naming collision (two machines both delivered
    # BATCH_01_file_00005.pptx with different content)
    name_collisions: list[tuple[FileRecord, FileRecord]] = field(default_factory=list)
    unique_a: list[FileRecord] = field(default_factory=list)
    unique_b: list[FileRecord] = field(default_factory=list)

    @property
    def all_hashes(self) -> list[str]:
        """Sorted union of every distinct sha256 seen in either folder.
        Feeds `pptxsweeper import-catalog` so those files are never
        re-downloaded on this machine."""
        return sorted({r.sha256 for r in self.a_records} | {r.sha256 for r in self.b_records})


def compare_folders(folder_a: str, folder_b: str,
                    a_records: list[FileRecord], b_records: list[FileRecord]) -> CompareResult:
    by_hash_a: dict[str, FileRecord] = {r.sha256: r for r in a_records}
    by_hash_b: dict[str, FileRecord] = {r.sha256: r for r in b_records}

    duplicates = [
        (by_hash_a[h], by_hash_b[h])
        for h in sorted(set(by_hash_a) & set(by_hash_b))
    ]

    by_name_a: dict[str, FileRecord] = {r.delivered_filename: r for r in a_records}
    by_name_b: dict[str, FileRecord] = {r.delivered_filename: r for r in b_records}
    name_collisions = [
        (by_name_a[n], by_name_b[n])
        for n in sorted(set(by_name_a) & set(by_name_b))
        if by_name_a[n].sha256 != by_name_b[n].sha256
    ]

    hashes_a = set(by_hash_a)
    hashes_b = set(by_hash_b)
    unique_a = [r for r in a_records if r.sha256 not in hashes_b]
    unique_b = [r for r in b_records if r.sha256 not in hashes_a]

    return CompareResult(folder_a=folder_a, folder_b=folder_b,
                         a_records=a_records, b_records=b_records,
                         duplicates=duplicates, name_collisions=name_collisions,
                         unique_a=unique_a, unique_b=unique_b)


# --------------------------------------------------------------------------
def write_duplicates_csv(result: CompareResult, out_path: Path) -> Path:
    """One row per exact duplicate pair."""
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["sha256", "name_in_A", "name_in_B",
                         "size_A", "size_B", "source_url_A", "source_url_B"])
        for ra, rb in result.duplicates:
            writer.writerow([ra.sha256, ra.delivered_filename, rb.delivered_filename,
                             ra.size, rb.size, ra.source_url, rb.source_url])
    return out_path


def write_catalog_csv(result: CompareResult, out_path: Path) -> Path:
    """sha256 column -> directly consumable by `pptxsweeper import-catalog`."""
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["sha256"])
        for sha in result.all_hashes:
            writer.writerow([sha])
    return out_path


def summarize(result: CompareResult) -> dict:
    return {
        "folder_a": result.folder_a,
        "folder_b": result.folder_b,
        "files_in_a": len(result.a_records),
        "files_in_b": len(result.b_records),
        "exact_duplicates": len(result.duplicates),
        "name_collisions": len(result.name_collisions),
        "unique_to_a": len(result.unique_a),
        "unique_to_b": len(result.unique_b),
        "unique_hashes_total": len(result.all_hashes),
    }


def sidecar_reader(folder_dir: Path) -> list[dict]:
    """Load all *.metadata.json files from a local folder as dicts."""
    sidecars: list[dict] = []
    if not folder_dir.is_dir():
        return sidecars
    for path in sorted(folder_dir.rglob("*.metadata.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            sidecars.append(data)
    return sidecars
