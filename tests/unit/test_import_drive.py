"""Import-drive stage tests: reusing pre-computed feature vectors from a
Drive conversion folder, falling back to re-classify for unknown decks,
and idempotent re-runs."""
from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from pptxsweeper.db.dao import Registry


def _cfg(tmp_path: Path):
    from tests.unit.test_extract import _cfg as base_cfg
    cfg = base_cfg(tmp_path)
    cfg.raw["delivery"] = {
        "image": True,
        "import_drive": {
            "enabled": True,
            "folder": "PptxSweeper_Conversion",
            "vectors_file": str(tmp_path / "vectors.jsonl.gz"),
            "chunk_limit": 100,
            "min_free_disk_gb": 0,
        },
    }
    return cfg


def _write_vectors(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "vectors.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


class _FakeRclone:
    """Fake rclone rooted at root_folder: decks + sidecars on 'remote',
    download_file copies from the remote tree into local work dir."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "remote" / "PptxSweeper_Conversion"
        self.root.mkdir(parents=True, exist_ok=True)
        self.remote = "gdrive"
        self.root_folder = "PptxSweeper_Conversion"

    def check_remote_configured(self) -> bool:
        return True

    bin = "rclone"

    def lsjson(self) -> list[dict]:
        return [{"Name": f.name, "Size": f.stat().st_size}
                for f in sorted(self.root.iterdir()) if f.is_file()]

    def remote_path(self, *parts: str) -> str:
        return f"{self.remote}:{'/'.join(p for p in parts if p)}"

    def download_file(self, remote_parts: tuple[str, ...], local_dir: Path) -> None:
        src = self.root / remote_parts[0]
        if src.exists():
            dst = local_dir / src.name
            dst.write_bytes(src.read_bytes())

    # _list_decks shells out to `rclone lsjson -R`; tests monkeypatch the
    # stage's _list_decks to use this instead.
    def recursive_lsjson(self) -> list[dict]:
        out = []
        for f in sorted(self.root.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(self.root))
                out.append({"Name": rel, "Size": f.stat().st_size})
        return out


def _seed_remote(rclone: _FakeRclone, name: str, sidecar: dict) -> None:
    deck = rclone.root / name
    deck.parent.mkdir(parents=True, exist_ok=True)
    deck.write_bytes(b"PK\x03\x04fake-deck-bytes")
    stem = Path(name).stem
    (rclone.root / f"{stem}.metadata.json").write_text(json.dumps(sidecar))


def _patch_listing(stage, rclone):
    """Point the stage's recursive listing at the fake's tree."""
    stage._list_decks = lambda rc: [
        {"Name": e["Name"].rsplit("/", 1)[-1], "Path": e["Name"],
         "Size": e["Size"]}
        for e in rclone.recursive_lsjson()
        if e["Name"].lower().endswith((".pptx", ".ppt", ".pdf"))
    ]


def _sha256_of_deck(name: str) -> str:
    import hashlib
    return hashlib.sha256(b"PK\x03\x04fake-deck-bytes").hexdigest()


def test_import_with_vectors_hit(tmp_path, registry):
    """Deck with a matching vectors row -> registered with DELIVER +
    vectors pre-filled (extract picks it up, no classify)."""
    from pptxsweeper.stages.import_drive import ImportDriveStage
    cfg = _cfg(tmp_path)
    rclone = _FakeRclone(tmp_path)
    side = {"sha256": _sha256_of_deck("a.pptx"), "source_url": "https://x.edu/a.pptx",
            "source_domain": "x.edu", "format": "pptx", "slide_count": 12,
            "quality_class": "HIGH", "http_status": 200,
            "retrieval_method": "origin", "delivered_filename": "BATCH_01_file_00001.pptx",
            "original_filename": "a.pptx"}
    _seed_remote(rclone, "a.pptx", side)
    _write_vectors(tmp_path, [{"sha256": _sha256_of_deck("a.pptx"),
                               "feature_vectors": [{"native_chart_count": 2}],
                               "quality": "HIGH", "format": "pptx",
                               "slide_count": 12, "decision": "DELIVER"}])

    stage = ImportDriveStage(cfg, registry, dry_run=False)
    stage._rclone = lambda: rclone
    _patch_listing(stage, rclone)
    stats = stage.run()
    assert stats["imported"] == 1
    assert stats["vectors_hit"] == 1

    f = registry.conn.execute("SELECT * FROM files").fetchone()
    assert f["decision"] == "DELIVER"
    assert f["quality"] == "HIGH"
    vectors = json.loads(f["feature_vectors"])
    assert vectors[0]["native_chart_count"] == 2
    u = registry.conn.execute("SELECT * FROM urls").fetchone()
    assert u["status"] == "classified"     # extract can deliver directly
    assert u["url"] == "https://x.edu/a.pptx"
    meta = json.loads(u["metadata"])
    assert meta["drive_import"] is True
    assert Path(f["local_path"]).exists()


def test_import_vectors_miss_falls_back_to_classify(tmp_path, registry):
    """Deck with NO matching vectors -> url status 'downloaded' so the
    normal classify cycle computes vectors from the payload."""
    from pptxsweeper.stages.import_drive import ImportDriveStage
    cfg = _cfg(tmp_path)
    rclone = _FakeRclone(tmp_path)
    side = {"sha256": _sha256_of_deck("b.pptx"), "source_url": "https://x.edu/b.pptx",
            "source_domain": "x.edu", "format": "pptx"}
    _seed_remote(rclone, "b.pptx", side)
    _write_vectors(tmp_path, [])   # empty index

    stage = ImportDriveStage(cfg, registry, dry_run=False)
    stage._rclone = lambda: rclone
    _patch_listing(stage, rclone)
    stats = stage.run()
    assert stats["imported"] == 1
    assert stats["vectors_miss"] == 1
    u = registry.conn.execute("SELECT * FROM urls").fetchone()
    assert u["status"] == "downloaded"
    f = registry.conn.execute("SELECT * FROM files").fetchone()
    assert f["decision"] == "DELIVER"
    assert Path(f["local_path"]).exists()


def test_import_idempotent_second_run_skips(tmp_path, registry):
    from pptxsweeper.stages.import_drive import ImportDriveStage
    cfg = _cfg(tmp_path)
    rclone = _FakeRclone(tmp_path)
    side = {"sha256": _sha256_of_deck("c.pptx"), "source_url": "https://x.edu/c.pptx",
            "source_domain": "x.edu", "format": "pptx"}
    _seed_remote(rclone, "c.pptx", side)
    _write_vectors(tmp_path, [{"sha256": _sha256_of_deck("c.pptx"),
                               "feature_vectors": [{"table_count": 1}],
                               "quality": "MEDIUM", "format": "pptx",
                               "decision": "DELIVER"}])

    stage = ImportDriveStage(cfg, registry, dry_run=False)
    stage._rclone = lambda: rclone
    _patch_listing(stage, rclone)
    s1 = stage.run()
    assert s1["imported"] == 1
    s2 = stage.run()
    assert s2["imported"] == 0
    assert s2["skipped_existing"] == 1
    assert registry.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_import_existing_url_not_duplicated(tmp_path, registry):
    """A url already in the registry (from the live pipeline) is not
    re-inserted; INSERT OR IGNORE keeps the original row."""
    from pptxsweeper.stages.import_drive import ImportDriveStage
    cfg = _cfg(tmp_path)
    rclone = _FakeRclone(tmp_path)
    side = {"sha256": _sha256_of_deck("d.pptx"), "source_url": "https://y.edu/d.pptx",
            "source_domain": "y.edu", "format": "pptx"}
    _seed_remote(rclone, "d.pptx", side)
    _write_vectors(tmp_path, [{"sha256": _sha256_of_deck("d.pptx"),
                               "feature_vectors": [], "quality": "HIGH",
                               "format": "pptx", "decision": "DELIVER"}])
    with registry.tx():
        registry.conn.execute(
            "INSERT INTO urls (url, domain, tier, discovery_source, status) "
            "VALUES (?,?,?,?,'classified')",
            ("https://y.edu/d.pptx", "y.edu", 1, "live_pipeline"))

    stage = ImportDriveStage(cfg, registry, dry_run=False)
    stage._rclone = lambda: rclone
    _patch_listing(stage, rclone)
    stats = stage.run()
    assert stats["imported"] == 1   # file row still created under existing url
    u = registry.conn.execute("SELECT * FROM urls WHERE url=?", ("https://y.edu/d.pptx",)).fetchone()
    assert u["discovery_source"] == "live_pipeline"   # original row untouched
    f = registry.conn.execute("SELECT * FROM files").fetchone()
    assert f["url_id"] == u["id"]


def test_import_nested_subfolders(tmp_path, registry):
    """Decks living in BATCH_* subfolders under the conversion root are
    found by the recursive listing and downloaded from their real path."""
    from pptxsweeper.stages.import_drive import ImportDriveStage
    cfg = _cfg(tmp_path)
    rclone = _FakeRclone(tmp_path)
    side = {"sha256": _sha256_of_deck("n.pptx"), "source_url": "https://n.edu/n.pptx",
            "source_domain": "n.edu", "format": "pptx"}
    _seed_remote(rclone, "BATCH_01/n.pptx", side)
    _write_vectors(tmp_path, [{"sha256": _sha256_of_deck("n.pptx"),
                               "feature_vectors": [], "quality": "HIGH",
                               "format": "pptx", "decision": "DELIVER"}])

    stage = ImportDriveStage(cfg, registry, dry_run=False)
    stage._rclone = lambda: rclone
    _patch_listing(stage, rclone)
    stats = stage.run()
    assert stats["imported"] == 1
    f = registry.conn.execute("SELECT * FROM files").fetchone()
    assert Path(f["local_path"]).exists()
    assert f["original_filename"] == "n.pptx"


def test_import_cleanup_delivered_decks(tmp_path, registry):
    """Once a deck's pages are all terminal and its url is delivered, the
    local payload is deleted by the cleanup pass."""
    from pptxsweeper.stages.import_drive import ImportDriveStage
    cfg = _cfg(tmp_path)
    rclone = _FakeRclone(tmp_path)
    side = {"sha256": _sha256_of_deck("e.pptx"), "source_url": "https://z.edu/e.pptx",
            "source_domain": "z.edu", "format": "pptx"}
    _seed_remote(rclone, "e.pptx", side)
    _write_vectors(tmp_path, [{"sha256": _sha256_of_deck("e.pptx"),
                               "feature_vectors": [], "quality": "HIGH",
                               "format": "pptx", "decision": "DELIVER"}])

    stage = ImportDriveStage(cfg, registry, dry_run=False)
    stage._rclone = lambda: rclone
    _patch_listing(stage, rclone)
    stage.run()
    f = registry.conn.execute("SELECT * FROM files").fetchone()
    payload = Path(f["local_path"])
    assert payload.exists()

    # Simulate delivery: url delivered + pages terminal.
    with registry.tx():
        registry.conn.execute(
            "UPDATE urls SET status='delivered' WHERE id=?", (f["url_id"],))
        registry.conn.execute(
            "INSERT INTO pages (file_id, page_index, sha256, status, delivered_at) "
            "VALUES (?,?,?, 'delivered', '2026-08-18T00:00:00Z')",
            (f["id"], 0, "ab" * 32))

    stage.run()   # cleanup pass runs
    assert not payload.exists()
    f2 = registry.conn.execute("SELECT * FROM files WHERE id=?", (f["id"],)).fetchone()
    assert f2["local_path"] is None
