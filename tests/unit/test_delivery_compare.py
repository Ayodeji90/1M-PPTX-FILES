"""Tests for pptxsweeper/delivery_compare.py (cross-folder dedup logic)."""
from __future__ import annotations

import json

from pptxsweeper.delivery_compare import (
    FileRecord, compare_folders, load_records, parse_sidecar,
    sidecar_reader, write_catalog_csv, write_duplicates_csv,
)


def _sidecar(sha: str, name: str, url: str = "https://x.example/a.pptx",
             size: int = 100) -> dict:
    return {"sha256": sha, "delivered_filename": name,
            "source_url": url, "size": size}


def test_parse_sidecar_ok():
    rec = parse_sidecar(_sidecar("a" * 64, "BATCH_01_file_00001.pptx"))
    assert rec is not None
    assert rec.sha256 == "a" * 64
    assert rec.delivered_filename == "BATCH_01_file_00001.pptx"
    assert rec.size == 100


def test_parse_sidecar_missing_keys_is_none():
    assert parse_sidecar({}) is None
    assert parse_sidecar({"sha256": "x" * 64}) is None
    assert parse_sidecar({"delivered_filename": "x.pptx"}) is None


def test_parse_sidecar_normalizes_and_tolerates_bad_size():
    rec = parse_sidecar({**_sidecar("A" * 64, "f.pptx"), "size": "oops"})
    assert rec is not None
    assert rec.sha256 == "a" * 64   # lowercased
    assert rec.size == 0


def test_compare_folders_finds_duplicates_collisions_and_uniques():
    sha_dup = "d" * 64
    a = [
        FileRecord(sha256=sha_dup, delivered_filename="BATCH_01_file_00001.pptx"),
        FileRecord(sha256="a" * 64, delivered_filename="BATCH_01_file_00002.pptx"),
        FileRecord(sha256="x" * 64, delivered_filename="BATCH_01_file_00003.pptx"),
    ]
    b = [
        FileRecord(sha256=sha_dup, delivered_filename="BATCH_02_file_00001.pptx"),
        FileRecord(sha256="b" * 64, delivered_filename="BATCH_01_file_00002.pptx"),
        FileRecord(sha256="y" * 64, delivered_filename="BATCH_01_file_00003.pptx"),
    ]
    result = compare_folders("A", "B", a, b)
    # exact duplicate found despite different filenames
    assert len(result.duplicates) == 1
    ra, rb = result.duplicates[0]
    assert ra.sha256 == rb.sha256 == sha_dup
    assert ra.delivered_filename != rb.delivered_filename
    # same name, different content -> naming collision: both
    # BATCH_01_file_00002 and BATCH_01_file_00003 collide (files 00001
    # is the duplicate, so it must NOT be counted as a collision)
    assert len(result.name_collisions) == 2
    for ca, cb in result.name_collisions:
        assert ca.delivered_filename == cb.delivered_filename
        assert ca.sha256 != cb.sha256
    assert all(r.sha256 != sha_dup for pair in result.name_collisions
               for r in pair)
    # uniques
    assert len(result.unique_a) == 2
    assert len(result.unique_b) == 2
    # union catalog
    assert result.all_hashes == sorted({"d" * 64, "a" * 64, "x" * 64, "b" * 64, "y" * 64})


def test_compare_folders_no_overlap():
    a = [FileRecord(sha256="a" * 64, delivered_filename="BATCH_01_file_00001.pptx")]
    b = [FileRecord(sha256="b" * 64, delivered_filename="BATCH_01_file_00001.pptx")]
    result = compare_folders("A", "B", a, b)
    assert result.duplicates == []
    assert len(result.name_collisions) == 1   # same name, different content
    assert result.all_hashes == ["a" * 64, "b" * 64]


def test_compare_folders_identical_sets():
    rec = FileRecord(sha256="a" * 64, delivered_filename="BATCH_01_file_00001.pptx")
    result = compare_folders("A", "B", [rec], [rec])
    assert len(result.duplicates) == 1
    assert result.name_collisions == []
    assert result.unique_a == [] and result.unique_b == []
    assert result.all_hashes == ["a" * 64]


def test_load_records_drops_unusable():
    records = load_records([
        _sidecar("a" * 64, "f1.pptx"),
        {},                                   # dropped
        {"sha256": "b" * 64},                 # dropped (no filename)
    ])
    assert len(records) == 1
    assert records[0].sha256 == "a" * 64


def test_sidecar_reader(tmp_path):
    (tmp_path / "BATCH_01_file_00001.metadata.json").write_text(
        json.dumps(_sidecar("a" * 64, "BATCH_01_file_00001.pptx")), encoding="utf-8")
    (tmp_path / "broken.metadata.json").write_text("{not json", encoding="utf-8")
    sidecars = sidecar_reader(tmp_path)
    assert len(sidecars) == 1
    assert sidecars[0]["sha256"] == "a" * 64


def test_csv_outputs_roundtrip(tmp_path):
    dup = FileRecord(sha256="d" * 64, delivered_filename="A.pptx",
                     source_url="https://a.example/a.pptx")
    result = compare_folders("A", "B", [dup], [dup])
    dup_csv = write_duplicates_csv(result, tmp_path / "dups.csv")
    assert "d" * 64 in dup_csv.read_text(encoding="utf-8")
    cat_csv = write_catalog_csv(result, tmp_path / "catalog.csv")
    text = cat_csv.read_text(encoding="utf-8")
    assert text.splitlines() == ["sha256", "d" * 64]
