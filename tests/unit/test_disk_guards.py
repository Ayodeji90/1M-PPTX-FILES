"""Disk-guard unit tests: the OS-wedge prevention added after the VM
became unreachable (full disk from unguarded review/staging dirs)."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from pptxsweeper.stages.classify_stage import review_prune_limit


# ----------------------------------------------------------------------
# review_prune_limit: min(configured cap, disk-safe bound)
# ----------------------------------------------------------------------
def test_cap_used_when_disk_plenty():
    # 8 GB cap, 20 GB free, 4 GB hard floor -> disk_safe=13GB -> cap wins
    assert review_prune_limit(8, free_gb=20, hard_min_gb=4) == 8 * 1024 ** 3


def test_disk_safe_bound_when_cap_too_big():
    # THE production bug: cap 40 GB on a 30 GB disk can never trigger,
    # so review/ grows until the disk fills and wedges the OS. With
    # 10 GB free the bound is (10-4-3)=3 GB, not 40.
    assert review_prune_limit(40, free_gb=10, hard_min_gb=4) == 3 * 1024 ** 3


def test_zero_when_disk_critical():
    # at the hard floor there is no room for review/ at all -> prune to 0
    assert review_prune_limit(8, free_gb=4, hard_min_gb=4) == 0


def test_never_exceeds_cap():
    assert review_prune_limit(1, free_gb=500, hard_min_gb=2) == 1 * 1024 ** 3


# ----------------------------------------------------------------------
# Orchestrator hard floor + reclaim sweep
# ----------------------------------------------------------------------
class _FakeCfg:
    def __init__(self, raw: dict, data_dir: Path):
        self.raw = raw
        self._data_dir = data_dir

    def path(self, section: str, key: str) -> Path:
        if key == "data_dir":
            return self._data_dir
        if key == "download_tmp_dir":
            return self._data_dir / "tmp_downloads"
        if key == "logs_dir":
            return self._data_dir / "logs"
        raise KeyError(key)


def _orchestrator(cfg) -> object:
    from pptxsweeper.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)   # skip heavy __init__
    orch.cfg = cfg
    return orch


def test_hard_floor_blocks_stages_when_disk_low(tmp_path, monkeypatch):
    import pptxsweeper.orchestrator as orch_mod
    orch = _orchestrator(_FakeCfg({"disk": {"hard_min_free_gb": 4}}, tmp_path))
    monkeypatch.setattr(orch_mod, "free_gb", lambda _p: 5.0)
    assert orch._disk_ok() is True
    monkeypatch.setattr(orch_mod, "free_gb", lambda _p: 3.5)
    assert orch._disk_ok() is False
    # missing config -> default floor of 2 GB still enforced
    orch.cfg = _FakeCfg({}, tmp_path)
    monkeypatch.setattr(orch_mod, "free_gb", lambda _p: 1.0)
    assert orch._disk_ok() is False


def _mk_orch(tmp_path):
    orch = _orchestrator(_FakeCfg({"disk": {"reclaim_max_age_h": 24}}, tmp_path))
    orch.db_path = tmp_path / "registry.db"
    return orch


def test_reclaim_deletes_stale_parts_and_keeps_fresh(tmp_path):
    orch = _mk_orch(tmp_path)
    tmp = tmp_path / "tmp_downloads"
    logs = tmp_path / "logs"
    tmp.mkdir()
    logs.mkdir()
    stale = tmp / "1.part"
    fresh = tmp / "2.part"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))
    for i in range(1, 7):                   # 6 rotated logs, keep newest 5
        p = logs / f"download.jsonl.{i}"
        p.write_bytes(b"x")
        os.utime(p, (old, old))
    os.utime(logs / "download.jsonl.6", (time.time(), time.time()))
    orch._reclaim_disk()
    assert not stale.exists()                # old .part reclaimed
    assert fresh.exists()                    # recent .part kept
    kept = sorted(p.name for p in logs.iterdir())
    assert len(kept) == 5                    # oldest rotated log dropped


# ----------------------------------------------------------------------
# _should_pause: backlog + free-RAM gates with hysteresis
# ----------------------------------------------------------------------
def test_pause_reasons_backlog_gate():
    from pptxsweeper.download.worker import _should_pause
    r = set()
    assert _should_pause(r, backlog_count=1500, backlog_cap=1500,
                         ram_free_gb=8.0, ram_min_gb=1.5) == {"backlog"}
    # hysteresis: stays paused until count drops to 80% of cap (1200)
    assert _should_pause(r, 1250, 1500, 8.0, 1.5) == {"backlog"}
    assert _should_pause(r, 1100, 1500, 8.0, 1.5) == set()


def test_pause_reasons_ram_gate():
    from pptxsweeper.download.worker import _should_pause
    r = set()
    assert _should_pause(r, 0, 1500, ram_free_gb=0.8, ram_min_gb=1.5) == {"ram"}
    # hysteresis: resume needs 1.2x headroom, so 1.5GB still paused
    assert _should_pause(r, 0, 1500, 1.5, 1.5) == {"ram"}
    assert _should_pause(r, 0, 1500, 2.0, 1.5) == set()


def test_pause_reasons_combine_and_clear():
    from pptxsweeper.download.worker import _should_pause
    r = _should_pause(set(), 1500, 1500, 0.8, 1.5)   # both gates hit
    assert r == {"backlog", "ram"}
    r = _should_pause(r, 1100, 1500, 2.0, 1.5)        # both cleared
    assert r == set()
    # gates disabled (cap 0 / min 0) never add reasons
    assert _should_pause(set(), 99999, 0, 0.1, 0) == set()


def test_reclaim_deletes_orphaned_payloads_keeps_live(tmp_path, registry):
    orch = _mk_orch(tmp_path)
    registry.conn.execute(
        "INSERT INTO urls (url, domain, tier, discovery_source, status, sha256) "
        "VALUES (?,?,?,?,?,?)",
        ("http://x/1", "x.com", 1, "t", "downloaded", "def456"))
    registry.conn.commit()
    tmp = tmp_path / "tmp_downloads"
    tmp.mkdir()
    old = time.time() - 48 * 3600
    orphan = tmp / "abc123.pptx"
    live = tmp / "def456.pptx"
    orphan.write_bytes(b"x")
    live.write_bytes(b"x")
    os.utime(orphan, (old, old))
    os.utime(live, (old, old))
    orch._reclaim_disk()
    assert not orphan.exists()   # sha not in any live url row -> reclaimed
    assert live.exists()         # sha still owned by a 'downloaded' row
