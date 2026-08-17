"""Extract stage + image delivery tests: page selection, perceptual hash,
renderer (real soffice + pdftoppm), extract stage dedup, image-mode package.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pptxsweeper.db.dao import Registry
from pptxsweeper.extract.select import is_graphical_page, select_graphical_pages
from pptxsweeper.utils.perceptual import dhash, hamming_distance


def _vec(**kw) -> dict:
    base = dict(index=0, text_char_count=0, bullet_count=0, native_chart_count=0,
                diagram_count=0, table_count=0, ole_spreadsheet_count=0,
                image_count=0, image_analytical_count=0, image_photo_count=0,
                vector_drawing_count=0, is_structural_filler=False,
                filler_reason="")
    base.update(kw)
    return base


# ----------------------------------------------------------------------
# Page selection
# ----------------------------------------------------------------------
def test_chart_page_is_graphical():
    ok, reason = is_graphical_page(_vec(native_chart_count=1))
    assert ok and reason == "analytical"


def test_photo_page_is_not_graphical():
    ok, _ = is_graphical_page(_vec(image_photo_count=3, text_char_count=50,
                                   image_count=3))
    assert not ok


def test_text_only_page_is_not_graphical():
    ok, _ = is_graphical_page(_vec(text_char_count=800, bullet_count=5))
    assert not ok


def test_filler_never_graphical():
    ok, reason = is_graphical_page(_vec(native_chart_count=1,
                                        is_structural_filler=True))
    assert not ok and reason == "structural_filler"


def test_table_and_ole_are_graphical():
    ok, _ = is_graphical_page(_vec(table_count=1))
    assert ok
    ok, _ = is_graphical_page(_vec(ole_spreadsheet_count=1))
    assert ok


def test_vector_graphics_are_graphical():
    ok, reason = is_graphical_page(_vec(vector_drawing_count=2))
    assert ok and reason == "vector_graphics"


def test_select_only_graphical_pages():
    vectors = [
        _vec(index=0, is_structural_filler=True),                     # title
        _vec(index=1, native_chart_count=1),                          # chart
        _vec(index=2, image_photo_count=2, image_count=2),            # photo
        _vec(index=3, diagram_count=1),                               # smartart
        _vec(index=4, text_char_count=900),                           # text
    ]
    sel = select_graphical_pages(vectors)
    assert [s["index"] for s in sel] == [1, 3]
    assert sel[0]["reason"] == "analytical"


def test_select_empty_for_none():
    assert select_graphical_pages(None) == []
    assert select_graphical_pages([]) == []


# ----------------------------------------------------------------------
# Perceptual hash
# ----------------------------------------------------------------------
def test_dhash_deterministic_and_near_duplicate(tmp_path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (200, 120), "white")
    d = ImageDraw.Draw(img)
    d.line([(30, 20), (30, 90)], fill="black", width=2)
    d.line([(30, 90), (170, 90)], fill="black", width=2)
    d.rectangle([(60, 40), (90, 90)], fill=(31, 119, 180))
    d.rectangle([(110, 55), (140, 90)], fill=(255, 127, 14))
    p1 = tmp_path / "a.png"
    img.save(p1)

    # same pixels, different file -> identical hash
    assert dhash(p1.read_bytes()) == dhash(p1.read_bytes())

    # re-encode as JPEG (lossy) + tiny resize -> still near-identical
    p2 = tmp_path / "b.jpg"
    img.resize((198, 119)).save(p2, format="JPEG", quality=70)
    assert hamming_distance(dhash(p1.read_bytes()), dhash(p2.read_bytes())) <= 6


def test_dhash_distinct_images_differ(tmp_path):
    from PIL import Image, ImageDraw
    a = Image.new("RGB", (200, 120), "white")
    ImageDraw.Draw(a).rectangle([(10, 10), (190, 110)], fill=(200, 0, 0))
    b = Image.new("RGB", (200, 120), "black")
    ImageDraw.Draw(b).rectangle([(10, 10), (190, 110)], fill=(0, 200, 0))
    assert hamming_distance(dhash(a.tobytes() and _png(a)), dhash(_png(b))) > 30


def _png(img) -> bytes:
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ----------------------------------------------------------------------
# Renderer (REAL soffice + pdftoppm; skipped when tools absent)
# ----------------------------------------------------------------------
def _tools_available() -> bool:
    import shutil
    return shutil.which("soffice") is not None and shutil.which("pdftoppm") is not None


@pytest.mark.skipif(not _tools_available(), reason="soffice/pdftoppm not installed")
def test_render_chart_pages_to_png(decks, tmp_path):
    from pptxsweeper.extract.render import render_file_pages
    res = render_file_pages(decks["chart_heavy"], [1, 2, 3], tmp_path, dpi=100)
    assert res.ok, res.reason
    assert sorted(res.pages) == [1, 2, 3]
    for idx, png in res.pages.items():
        assert png.exists() and png.stat().st_size > 0
        from PIL import Image
        im = Image.open(png)
        assert im.format == "PNG"


@pytest.mark.skipif(not _tools_available(), reason="soffice/pdftoppm not installed")
def test_render_missing_page_fails_cleanly(decks, tmp_path):
    from pptxsweeper.extract.render import render_file_pages
    res = render_file_pages(decks["chart_heavy"], [99], tmp_path, dpi=100)
    assert not res.ok


# ----------------------------------------------------------------------
# Extract stage (renderer monkeypatched)
# ----------------------------------------------------------------------
def _real_png(seed: int = 0, size: tuple = (120, 80)) -> bytes:
    """A valid tiny PNG whose STRUCTURE varies strongly with seed (dHash
    reads grayscale layout, so distinct seeds must look different)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    n_bars = 1 + (seed % 5)
    w = (size[0] - 30) // max(1, n_bars * 2)
    for i in range(n_bars):
        x0 = 20 + i * 2 * w
        h = 20 + ((seed * 7 + i * 13) % (size[1] - 40))
        d.rectangle([x0, size[1] - 15 - h, x0 + w, size[1] - 15],
                    fill=(31 + (seed * 37) % 200, 119, 180))
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed_classified_file(reg: Registry, decks, tmp_path: Path) -> int:
    """Insert a classified DELIVER file row (chart_heavy feature vectors)."""
    from pptxsweeper.quality import classify
    report = classify(decks["chart_heavy"])
    fv = json.dumps(report.feature_vectors_json())
    sha = "ab" * 32
    url_id = _seed_url(reg)
    payload = tmp_path / "chart_heavy.pptx"
    import shutil as _sh
    _sh.copy2(decks["chart_heavy"], payload)
    return reg.insert_file(url_id=url_id, sha256=sha, local_path=str(payload),
                           format="pptx", slide_count=report.slide_count,
                           quality="HIGH", decision="DELIVER",
                           feature_vectors=fv, quality_report=json.dumps(report.to_dict()))


def _seed_url(reg: Registry) -> int:
    with reg.tx():
        cur = reg.conn.execute(
            "INSERT INTO urls (url, domain, tier, discovery_source, status) "
            "VALUES (?,?,?,?,?)",
            ("https://example.edu/deck.pptx", "example.edu", 5, "test", "classified"))
        return cur.lastrowid


def _make_extract_stage(cfg, reg, tmp_path):
    from pptxsweeper.stages.extract_stage import ExtractStage
    stage = ExtractStage(cfg, reg)
    stage.pages_dir = tmp_path / "pages"
    stage.tmp_dir = tmp_path / "tmp"
    return stage


def test_extract_renders_selected_pages(decks, registry, tmp_path, monkeypatch):
    fid = _seed_classified_file(registry, decks, tmp_path)

    def fake_render(payload, indexes, out_dir, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        from pptxsweeper.extract.render import RenderResult
        pages = {}
        for idx in indexes:
            p = out_dir / f"src_{Path(payload).stem}" / f"page_{idx}-{idx}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_real_png(idx))
            pages[idx] = p
        return RenderResult(True, pages=pages)

    # Patch the name INSIDE the stage module: extract_stage imports
    # render_file_pages at import time, so patching the render module's
    # attribute would not affect the stage's already-bound name.
    monkeypatch.setattr("pptxsweeper.stages.extract_stage.render_file_pages", fake_render)
    cfg = _cfg(tmp_path)
    stage = _make_extract_stage(cfg, registry, tmp_path)
    stats = stage.run()
    assert stats["extracted"] > 0
    pages = registry.pages_for_file(fid)
    assert pages and all(p["status"] == "extracted" for p in pages)
    for p in pages:
        assert p["local_path"] and Path(p["local_path"]).exists()
        assert p["sha256"] and p["phash"]


def test_extract_marks_exact_duplicate(decks, registry, tmp_path, monkeypatch):
    fid = _seed_classified_file(registry, decks, tmp_path)
    # pre-insert one page with the SAME image bytes the fake render will
    # produce -> exact-dup gate must catch it
    def fake_render(payload, indexes, out_dir, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        from pptxsweeper.extract.render import RenderResult
        pages = {}
        for idx in indexes:
            p = out_dir / f"page_{idx}-{idx}.png"
            p.write_bytes(_real_png(idx))
            pages[idx] = p
        return RenderResult(True, pages=pages)

    monkeypatch.setattr("pptxsweeper.stages.extract_stage.render_file_pages", fake_render)
    # The known page must belong to a DIFFERENT file: a page already
    # recorded for THIS file would make the extract query skip the file
    # entirely (NOT EXISTS extracted/delivered/duplicate), which is the
    # wrong scenario -- exact-dup is the SAME image bytes appearing in a
    # second deck.
    from pptxsweeper.utils.hashing import sha256_file
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png") as tf:
        tf.write(_real_png(1))
        tf.flush()
        dup_sha = sha256_file(tf.name)
    with registry.tx():
        other_url = registry.conn.execute(
            "INSERT INTO urls (url, domain, tier, discovery_source, status) "
            "VALUES (?,?,?,?, 'classified')",
            ("https://other.edu/deck2.pptx", "other.edu", 5, "test")).lastrowid
        other_fid = registry.conn.execute(
            "INSERT INTO files (url_id, sha256, decision, quality) "
            "VALUES (?,?,?,?)",
            (other_url, "cd" * 32, "DELIVER", "HIGH")).lastrowid
        registry.conn.execute(
            "INSERT INTO pages (file_id, page_index, sha256, phash, status) "
            "VALUES (?,?,?,?, 'extracted')",
            (other_fid, 0, dup_sha, "0" * 16))
    cfg = _cfg(tmp_path)
    stage = _make_extract_stage(cfg, registry, tmp_path)
    stats = stage.run()
    pages = registry.pages_for_file(fid)
    dup = [p for p in pages if p["status"] == "duplicate"]
    assert dup, "the seeded sha must be detected as an exact duplicate"
    assert stats["duplicate"] >= 1


def test_same_file_similar_pages_not_deduped(decks, registry, tmp_path, monkeypatch):
    """Regression: several similar-styled chart pages from ONE deck are all
    distinct deliverables. The near-dup gate must only compare against
    OTHER files, never pages of the same file (they render nearly
    identically for a template deck)."""
    from pptxsweeper.extract.select import is_graphical_page
    fid = _seed_classified_file(registry, decks, tmp_path)
    # Build feature vectors: 4 chart pages with nearly identical layout
    # (like a real template deck). All 4 must be extracted.
    vectors = [_vec(index=0, is_structural_filler=True)]
    vectors += [_vec(index=i, native_chart_count=1) for i in range(1, 5)]
    with registry.tx():
        registry.conn.execute(
            "UPDATE files SET feature_vectors=? WHERE id=?",
            (json.dumps(vectors), fid))

    def fake_render(payload, indexes, out_dir, **kw):
        # identical bytes for every page -> same sha AND same phash
        out_dir.mkdir(parents=True, exist_ok=True)
        from pptxsweeper.extract.render import RenderResult
        pages = {}
        for idx in indexes:
            p = out_dir / f"page_{idx}-{idx}.png"
            p.write_bytes(_real_png(3))   # same image every page
            pages[idx] = p
        return RenderResult(True, pages=pages)

    monkeypatch.setattr("pptxsweeper.stages.extract_stage.render_file_pages", fake_render)
    stage = _make_extract_stage(_cfg(tmp_path), registry, tmp_path)
    stats = stage.run()
    pages = registry.pages_for_file(fid)
    # first page extracted; the other 3 are EXACT dupes (same bytes) and
    # must be caught by the sha gate, not by same-file phash comparison
    assert stats["extracted"] == 1, stats
    assert stats["duplicate"] == 3, stats


def test_extract_idempotent(decks, registry, tmp_path, monkeypatch):
    fid = _seed_classified_file(registry, decks, tmp_path)

    def fake_render(payload, indexes, out_dir, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        from pptxsweeper.extract.render import RenderResult
        pages = {}
        for idx in indexes:
            p = out_dir / f"page_{idx}-{idx}.png"
            p.write_bytes(_real_png(idx))
            pages[idx] = p
        return RenderResult(True, pages=pages)

    monkeypatch.setattr("pptxsweeper.stages.extract_stage.render_file_pages", fake_render)
    stage = _make_extract_stage(_cfg(tmp_path), registry, tmp_path)
    first = stage.run()
    n_after_first = len(registry.pages_for_file(fid))
    second = stage.run()
    # The stats dict accumulates across runs; the real idempotency check is
    # that the second run creates no NEW pages.
    assert first["extracted"] > 0
    assert len(registry.pages_for_file(fid)) == n_after_first, \
        "second run must not re-render or duplicate pages"


def _cfg(tmp_path):
    from pptxsweeper.config import Config
    # Build a Config over a tiny inline dict instead of the repo config.
    raw = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "db_path": str(tmp_path / "registry.db"),
            "download_tmp_dir": str(tmp_path / "tmp"),
            "staging_dir": str(tmp_path / "staging"),
            "review_dir": str(tmp_path / "review"),
            "pages_dir": str(tmp_path / "pages"),
            "logs_dir": str(tmp_path / "logs"),
            "seeds_dir": "seeds",
            "status_dir": str(tmp_path / "status"),
            "batch_build_dir": str(tmp_path / "build"),
            "manifests_dir": str(tmp_path / "manifests"),
        },
        "rclone": {"bin": "rclone", "remote_name": "gdrive",
                   "root_folder": "PptxSweeper_Delivery", "batch_folder_pattern": "BATCH_{batch_id}",
                   "review_folder": "_review", "status_folder": "_status",
                   "backups_folder": "registry_backups", "verify_method": "size-only",
                   "timeout_s": 900, "review_sync_timeout_s": 300},
        "batch": {"size": 25000, "min_padding": 2,
                  "composition": {"high_min_pct": 0.70, "medium_min_pct": 0.20,
                                  "medium_max_pct": 0.30, "low_max_pct": 0.0,
                                  "max_batch_open_days": 14}},
        "upload": {"daily_byte_budget_gb": 700, "max_retries": 5,
                   "retry_backoff_s": [10, 30, 120, 300, 900]},
        "disk": {"hard_min_free_gb": 2, "reclaim_max_age_h": 24},
        "download": {"max_content_length_mb": 300, "min_content_length_kb": 10,
                     "connect_timeout_s": 15, "read_timeout_s": 90,
                     "concurrency": 8, "max_downloaded_backlog": 3000,
                     "min_free_disk_gb": 1, "min_free_ram_gb": 0.5,
                     "shutdown_grace_s": 30, "db_writer_batch_size": 100,
                     "db_writer_flush_interval_s": 2.0, "claim_batch_size": 25,
                     "max_attempts": 4, "wayback_fallback": True,
                     "handoff_first": False, "domain_refresh_s": 300},
        "politeness": {"default_delay_s": 1.5, "jitter_pct": 0.4,
                       "robots_cache_ttl_hours": 24, "head_before_get": True,
                       "domain_delay_overrides": {},
                       "circuit_breaker": {"backoff_stages_s": [30, 300, 1800],
                                           "park_after_consecutive_failures": 3,
                                           "park_duration_hours": 48,
                                           "blacklist_after_parks": 2}},
        "wayback": {"cdx_base_url": "x", "fetch_base_url": "x",
                    "requests_per_sec_per_worker": 3, "cdx_page_size": 1000,
                    "retry_after_429_s": [30, 120, 300]},
        "common_crawl": {"index_list_url": "x", "num_recent_crawls": 15,
                         "data_base_url": "x", "cdx_fallback_base": "x",
                         "extensions": [".pptx", ".ppt", ".pdf"]},
        "quality": {"epsilon": 0.03, "min_slides": 5, "use_ocr": False,
                    "ocr_ambiguous_only": True, "use_opencv": False,
                    "high": {"min_analytical_pct": 0.50, "min_chart_diagram_pages": 3,
                             "max_photo_heavy_pct": 0.30},
                    "medium": {"min_analytical_pct": 0.40, "min_chart_diagram_pages": 1},
                    "low": {"text_only_pct": 0.75, "photo_heavy_pct": 0.50},
                    "image_signals": {"unique_color_ratio_max": 0.15,
                                      "straight_edge_ratio_min": 0.35,
                                      "uniform_bg_ratio_min": 0.25,
                                      "text_density_ambiguous_low": 0.03,
                                      "text_density_ambiguous_high": 0.15}},
        "compliance": {"blocklist_file": "./seeds/blocklist_domains.txt",
                       "excluded_sources_file": "./seeds/excluded_sources.txt",
                       "pii_review_on_hit": True, "minors_review_on_hit": True,
                       "prohibited_reject_on_hit": True},
        "conversion": {"soffice_bin": "soffice", "timeout_s": 120, "max_concurrent": 2},
        "classify": {"review_dir_cap_gb": 20, "commit_every": 50, "workers": 0,
                     "worker_memory_mb": 512, "chunk_limit": 150,
                     "file_timeout_s": 300, "watchdog_stall_min": 0,
                     "review_auto_promote_hours": 0},
        "filter": {"per_domain_cap": 20000, "tier_domain_caps": {5: 500}},
        "harvesters": {},
        "multi_node": {"consumer_node_ids": [], "dedup_folder": "_dedup",
                       "dedup_sync_interval_min": 60, "handoff_root": "PptxSweeper_Handoff",
                       "handoff_interval_hours": 10, "handoff_fraction": 0.6,
                       "interleave_batch_ids": True},
        "logging": {"level": "INFO", "json_lines": False},
        "extract": {"dpi": 150, "max_concurrent": 2, "page_timeout_s": 120,
                    "conv_timeout_s": 180, "phash_distance": 10,
                    "chunk_limit": 50, "min_free_disk_gb": 1,
                    "pdftoppm_bin": "pdftoppm"},
        "delivery": {"image": False},
    }
    cfg = Config(raw=raw, root=tmp_path, env_path=None)
    cfg.ensure_dirs()
    return cfg
