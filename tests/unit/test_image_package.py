"""Image-mode packaging tests: page candidates, img naming, page
mark-delivered, manifest/audit page columns."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pptxsweeper.db.dao import Registry
from pptxsweeper.naming import BatchAllocator, delivered_filename
from pptxsweeper.node import NodeIdentity


# ----------------------------------------------------------------------
# Naming
# ----------------------------------------------------------------------
def test_image_filename_img_prefix_png():
    assert delivered_filename(4, 1, "png", prefix="img") == "BATCH_04_img_00001.png"
    assert delivered_filename(4, 1, "png") == "BATCH_04_file_00001.png"


def test_png_allowed_extension():
    assert delivered_filename(1, 7, "png", prefix="img").endswith(".png")


def test_assign_page_filename_sequential_and_idempotent(registry):
    alloc = BatchAllocator(registry)
    batch = alloc.open_batch()
    with registry.tx():
        fid = registry.conn.execute(
            "INSERT INTO files (sha256, decision, quality) VALUES (?,?,?)",
            ("aa" * 32, "DELIVER", "HIGH")).lastrowid
        pid1 = registry.conn.execute(
            "INSERT INTO pages (file_id, page_index, sha256, status) VALUES (?,?,?,?)",
            (fid, 0, "b" * 64, "extracted")).lastrowid
        pid2 = registry.conn.execute(
            "INSERT INTO pages (file_id, page_index, sha256, status) VALUES (?,?,?,?)",
            (fid, 1, "c" * 64, "extracted")).lastrowid
    n1 = alloc.assign_page_filename(batch["batch_id"], pid1, "png", prefix="img")
    n1r = alloc.assign_page_filename(batch["batch_id"], pid1, "png", prefix="img")
    assert n1 == n1r == "BATCH_01_img_00001.png"
    n2 = alloc.assign_page_filename(batch["batch_id"], pid2, "png", prefix="img")
    assert n2 == "BATCH_01_img_00002.png"


# ----------------------------------------------------------------------
# Page candidates (compose)
# ----------------------------------------------------------------------
def _seed(reg: Registry) -> tuple[int, int]:
    with reg.tx():
        url_id = reg.conn.execute(
            "INSERT INTO urls (url, domain, tier, discovery_source, status) "
            "VALUES (?,?,?,?, 'classified')",
            ("https://x.edu/report.pdf", "x.edu", 5, "test")).lastrowid
        fid = reg.conn.execute(
            "INSERT INTO files (url_id, sha256, decision, quality, local_path) "
            "VALUES (?,?,?,?,?)",
            (url_id, "aa" * 32, "DELIVER", "HIGH", "/tmp/nope.pdf")).lastrowid
        return url_id, fid


def test_deliverable_page_candidates(registry, tmp_path):
    from pptxsweeper.packager.compose import deliverable_page_candidates
    url_id, fid = _seed(registry)
    png = tmp_path / "p.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"q" * 50)
    with registry.tx():
        registry.conn.execute(
            "INSERT INTO pages (file_id, page_index, sha256, phash, local_path, status) "
            "VALUES (?,?,?,?,?, 'extracted')",
            (fid, 0, "b" * 64, "1" * 16, str(png)))
    rows = deliverable_page_candidates(registry)
    assert len(rows) == 1
    r = rows[0]
    assert r["file_id"] == fid and r["page_index"] == 0
    assert r["quality"] == "HIGH"
    assert r["source_url"] == "https://x.edu/report.pdf"
    assert r["image_sha256"] == "b" * 64
    assert r["file_size"] == png.stat().st_size


def test_deliverable_page_candidates_excludes_delivered(registry, tmp_path):
    from pptxsweeper.packager.compose import deliverable_page_candidates
    url_id, fid = _seed(registry)
    with registry.tx():
        registry.conn.execute(
            "INSERT INTO pages (file_id, page_index, sha256, status, delivered_at) "
            "VALUES (?,?,?, 'delivered', '2026-08-01T00:00:00Z')",
            (fid, 0, "b" * 64))
    assert deliverable_page_candidates(registry) == []


# ----------------------------------------------------------------------
# Image-mode package stage
# ----------------------------------------------------------------------
class _FakeRclone:
    """Mirrors Rclone: every path is rooted at root_folder (like a real
    rclone remote rooted there), so tests prove which Drive folder is used."""

    def __init__(self, tmp_path, root_folder=""):
        self.root = tmp_path / "remote"
        self.root_folder = root_folder
        self.root.mkdir(exist_ok=True)

    def _p(self, folder=""):
        return self.root / self.root_folder / folder if self.root_folder \
            else self.root / folder

    def mkdir(self, folder=""):
        self._p(folder).mkdir(parents=True, exist_ok=True)

    def copy_dir(self, local, folder, timeout=None, **kw):
        self.mkdir(folder)
        import shutil
        for f in Path(local).iterdir():
            if f.is_file():
                shutil.copy2(f, self._p(folder) / f.name)

    def lsjson(self, folder=""):
        d = self._p(folder)
        if not d.exists():
            return []
        return [{"Name": f.name, "Size": f.stat().st_size}
                for f in sorted(d.iterdir()) if f.is_file()]

    def check(self, local, folder, method="size-only", **kw):
        remote = {f.name: f.stat().st_size for f in self._p(folder).iterdir()}
        local_files = {f.name: f.stat().st_size for f in Path(local).iterdir() if f.is_file()}
        return all(remote.get(n) == s for n, s in local_files.items())

    def copy_file(self, src, folder):
        import shutil
        self.mkdir(folder)
        shutil.copy2(src, self._p(folder) / src.name)

    def remote_path(self, folder=""):
        return f"gdrive:{self.root_folder}/{folder}" if self.root_folder else f"gdrive:{folder}"


def _image_cfg(tmp_path, image_mode: bool = True):
    from tests.unit.test_extract import _cfg
    cfg = _cfg(tmp_path)
    cfg.raw["delivery"]["image"] = image_mode
    return cfg


def _seed_image_rows(reg: Registry, tmp_path: Path, n: int = 3) -> list[int]:
    """n HIGH pages across one file, all extracted with real PNGs on disk."""
    url_id, fid = _seed(reg)
    ids = []
    for i in range(n):
        png = tmp_path / "pages" / f"{fid}_{i:03d}.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([40 + i]) * 80)
        with reg.tx():
            pid = reg.conn.execute(
                "INSERT INTO pages (file_id, page_index, sha256, phash, local_path, status) "
                "VALUES (?,?,?,?,?, 'extracted')",
                (fid, i, f"{i:064x}", f"{i:016x}", str(png))).lastrowid
        ids.append(pid)
    return ids


def test_stream_upload_image_mode(tmp_path, registry):
    from pptxsweeper.packager.package_stage import PackageStage
    cfg = _image_cfg(tmp_path)
    pids = _seed_image_rows(registry, tmp_path, n=3)
    rclone = _FakeRclone(tmp_path)
    stage = PackageStage(cfg, registry, rclone=rclone, dry_run=False)
    stats = stage.stream_upload()
    assert stats["uploaded"] == 3
    rows = registry.conn.execute(
        "SELECT * FROM pages WHERE id IN (?,?,?)", tuple(pids)).fetchall()
    for r in rows:
        assert r["delivered_at"] is not None
        assert r["delivered_filename"].startswith("BATCH_01_img_")
        assert r["local_path"] is None
    # batch counts the images
    batch = registry.conn.execute("SELECT * FROM batches").fetchone()
    assert batch["file_count"] == 3
    # audit rows carry page identity
    audits = registry.conn.execute(
        "SELECT * FROM audit_log WHERE batch_id=?", (batch["batch_id"],)).fetchall()
    assert len(audits) == 3
    for a in audits:
        assert a["page_index"] is not None
        assert a["image_sha256"] and a["phash"]
        assert a["extraction_method"] == "libreoffice"
        assert a["format"] == "png"
    # the 3 PNGs (plus their provenance sidecars) are on Drive under their
    # delivered names; the manifest only uploads at batch finalize, which a
    # 3-image batch hasn't reached.
    remote_files = {f.name for f in (rclone.root / "BATCH_01").iterdir()}
    assert {"BATCH_01_img_00001.png", "BATCH_01_img_00002.png",
            "BATCH_01_img_00003.png"} <= remote_files
    # every image has its provenance sidecar alongside it on Drive
    for i in (1, 2, 3):
        assert f"BATCH_01_img_0000{i}.metadata.json" in remote_files


def test_batch_mode_image_names_assigned(tmp_path, registry):
    """Full-batch path (package without --stream) works in image mode."""
    from pptxsweeper.packager.package_stage import PackageStage
    cfg = _image_cfg(tmp_path)
    _seed_image_rows(registry, tmp_path, n=2)
    rclone = _FakeRclone(tmp_path)
    stage = PackageStage(cfg, registry, rclone=rclone, dry_run=False)
    res = stage.run(force=True)   # force: 2 images < batch size
    assert res["status"] == "finalized", res
    assert res["files"] == 2
    pages = registry.conn.execute("SELECT * FROM pages").fetchall()
    assert all(p["delivered_at"] is not None for p in pages)
    assert {p["delivered_filename"] for p in pages} == {
        "BATCH_01_img_00001.png", "BATCH_01_img_00002.png"}
    # finalized batch: manifest copied to Drive with page columns
    manifest = rclone.root / "BATCH_01" / "BATCH_01_manifest.csv"
    assert manifest.exists()
    content = manifest.read_text()
    assert "page_index" in content and "phash" in content
    assert "BATCH_01_img_00001.png" in content
    assert "BATCH_01_img_00002.png" in content


def test_image_mode_uses_own_drive_root(tmp_path, registry, monkeypatch):
    """Image mode must deliver into a NEW top-level Drive folder, never
    the deck delivery root -- the new phase gets its own folder per VM."""
    import pptxsweeper.packager.package_stage as ps_mod
    cfg = _image_cfg(tmp_path)
    monkeypatch.setenv("RCLONE_IMAGE_ROOT_FOLDER", "PptxSweeper_Image_VM9")
    _seed_image_rows(registry, tmp_path, n=1)

    captured = {}
    fake = _FakeRclone(tmp_path, root_folder="PptxSweeper_Image_VM9")

    def _fake_factory(*args, **kw):
        captured["root_folder"] = kw.get("root_folder")
        return fake

    monkeypatch.setattr(ps_mod, "Rclone", _fake_factory)
    from pptxsweeper.packager.package_stage import PackageStage
    stage = PackageStage(cfg, registry, dry_run=False)
    assert captured["root_folder"] == "PptxSweeper_Image_VM9", \
        "image mode must root deliveries at RCLONE_IMAGE_ROOT_FOLDER"
    res = stage.run(force=True)
    assert res["status"] == "finalized", res
    assert (fake.root / "PptxSweeper_Image_VM9" / "BATCH_01").exists()
    assert not (fake.root / "PptxSweeper_Delivery").exists()


def test_deck_mode_still_works(tmp_path, registry):
    """Non-image mode is untouched: file-based packaging still delivers."""
    from pptxsweeper.packager.package_stage import PackageStage
    cfg = _image_cfg(tmp_path, image_mode=False)
    with registry.tx():
        url_id = registry.conn.execute(
            "INSERT INTO urls (url, domain, tier, discovery_source, status) "
            "VALUES (?,?,?,?, 'classified')",
            ("https://x.edu/deck.pptx", "x.edu", 5, "test")).lastrowid
        payload = tmp_path / "staging" / "deck.pptx"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(b"PK\x03\x04" + b"d" * 100)
        registry.conn.execute(
            "INSERT INTO files (url_id, sha256, decision, quality, local_path, format) "
            "VALUES (?,?,?,?,?,?)",
            (url_id, "ab" * 32, "DELIVER", "HIGH", str(payload), "pptx"))
    rclone = _FakeRclone(tmp_path)
    stage = PackageStage(cfg, registry, rclone=rclone, dry_run=False)
    res = stage.run(force=True)
    assert res["status"] == "finalized"
    row = registry.conn.execute("SELECT * FROM files").fetchone()
    assert row["delivered_filename"].startswith("BATCH_01_file_")
    assert row["delivered_at"] is not None
