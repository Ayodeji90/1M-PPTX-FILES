"""Regression tests for the classify STALL watchdog + quarantine.

Production incident: a classify run on a 100-file chunk started at
15:32 and NEVER finished -- worker processes sat at 0% CPU for 50+
minutes. The per-file SIGALRM timeout cannot fire when a worker hangs
inside a C extension (lxml/PIL/numpy tight loop): signals are deferred
until the C call returns, which never happens. The old whole-run
watchdog (max_run_minutes=120) was far too slow, and when it did fire
it merely restarted -- re-picking the SAME pathological files forever.

New behavior: a progress-based stall watchdog. If a run makes ZERO
progress for `watchdog_stall_min` while tasks are still pending, it
quarantines every still-'downloaded' row (payload deleted, row
rejected -- they can never wedge a run again) and force-exits for a
clean orchestrator re-run.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

import pytest

from pptxsweeper.config import Config
from pptxsweeper.db.dao import Registry
from pptxsweeper.stages.classify_stage import ClassifyStage


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch):
    """Other tests set PPTXSWEEPER_OVERRIDE/PPTXSWEEPER_CONFIG; clear them
    so Config.load() here always reads the real config.yaml."""
    monkeypatch.delenv("PPTXSWEEPER_OVERRIDE", raising=False)
    monkeypatch.delenv("PPTXSWEEPER_CONFIG", raising=False)


def _insert_url(reg: Registry, url: str, status: str, payload: str) -> int:
    sha = hashlib.sha256(payload.encode()).hexdigest()
    with reg.tx():
        cur = reg.conn.execute(
            "INSERT INTO urls (url, domain, tier, discovery_source, status, "
            "sha256, metadata) VALUES (?,?,?,?,?,?,?)",
            (url, "example.com", 1, "test", status, sha,
             json.dumps({"local_path": payload})))
    return cur.lastrowid


def _make_stage(tmp_path, reg: Registry, stall_min: float) -> ClassifyStage:
    cfg = Config.load()
    cfg.raw["paths"]["data_dir"] = str(tmp_path)
    cfg.raw["paths"]["download_tmp_dir"] = str(tmp_path / "tmp")
    cfg.raw["paths"]["staging_dir"] = str(tmp_path / "staging")
    cfg.raw["paths"]["review_dir"] = str(tmp_path / "review")
    cfg.raw["classify"]["watchdog_stall_min"] = stall_min
    return ClassifyStage(cfg, reg)


def test_config_prefers_watchdog_stall_min(tmp_path):
    """The new progress-based key wins; the legacy whole-run key is the
    fallback only when the new one is absent."""
    reg = Registry(tmp_path / "registry.db")
    cfg = Config.load()
    cfg.raw["classify"].pop("watchdog_stall_min", None)
    cfg.raw["classify"]["max_run_minutes"] = 7
    stage = ClassifyStage(cfg, reg)
    assert stage.watchdog_stall_min == 7  # legacy fallback

    cfg2 = Config.load()
    cfg2.raw["classify"]["watchdog_stall_min"] = 5
    cfg2.raw["classify"]["max_run_minutes"] = 7
    stage2 = ClassifyStage(cfg2, reg)
    assert stage2.watchdog_stall_min == 5  # new key wins
    reg.close()


def test_quarantine_only_touches_still_downloaded(tmp_path, registry):
    """Rows still 'downloaded' are rejected + payload deleted; rows that
    already left 'downloaded' (already persisted) are never touched."""
    wedged = tmp_path / "wedged.pptx"
    wedged.write_bytes(b"x" * 10)
    done = tmp_path / "done.pptx"
    done.write_bytes(b"y" * 10)

    wid = _insert_url(registry, "https://example.com/wedged.pptx",
                      "downloaded", str(wedged))
    did = _insert_url(registry, "https://example.com/done.pptx",
                      "classified", str(done))  # already persisted -> untouched

    stage = _make_stage(tmp_path, registry, 0)
    stage._run_row_ids = [wid, did]
    stage._quarantine_inflight()

    assert not wedged.exists(), "wedged payload must be deleted"
    assert done.exists(), "already-classified payload must survive"
    row_w = registry.conn.execute(
        "SELECT status, reject_reason FROM urls WHERE id=?", (wid,)).fetchone()
    assert row_w["status"] == "rejected"
    assert row_w["reject_reason"] == "classify_watchdog_timeout"
    row_d = registry.conn.execute(
        "SELECT status FROM urls WHERE id=?", (did,)).fetchone()
    assert row_d["status"] == "classified"


def test_watchdog_fires_after_stall_and_quarantines(tmp_path, registry,
                                                    monkeypatch):
    """The stall watchdog force-exits when zero progress with pending
    tasks, and the wedged rows are rejected first (so the next run skips
    them)."""
    wedged = tmp_path / "wedged.pptx"
    wedged.write_bytes(b"z" * 10)
    wid = _insert_url(registry, "https://example.com/wedged.pptx",
                      "downloaded", str(wedged))

    stage = _make_stage(tmp_path, registry, 0.02)  # ~1.2s stall budget
    stage._done = threading.Event()  # not set -> keeps watching
    stage._last_progress = time.monotonic() - 600  # no progress for 10 min
    stage._remaining = 1
    stage._run_row_ids = [wid]

    # Don't actually die: raise instead so the test can assert the order.
    monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(
        SystemExit(code)))
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # don't wait 30s

    with pytest.raises(SystemExit) as exc:
        stage._watchdog()
    assert exc.value.code == 1
    assert not wedged.exists(), "quarantine must delete the wedged payload"
    status = registry.conn.execute(
        "SELECT status FROM urls WHERE id=?", (wid,)).fetchone()["status"]
    assert status == "rejected"


def test_watchdog_stays_silent_while_progressing(tmp_path, registry,
                                                 monkeypatch):
    """Progress resets the stall clock: a slow-but-working run never
    trips the watchdog (this is what makes it safe for slow decks)."""
    stage = _make_stage(tmp_path, registry, 0.5)
    stage._done = threading.Event()
    stage._remaining = 5
    fired = []

    real_time = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: real_time[0])
    stage._last_progress = real_time[0]

    # Simulate the main loop completing a task (resets progress) once the
    # watchdog's sleep window passes.
    def _simulate_main_work():
        real_time[0] += 60  # 1 min later, a task finishes
        stage._last_progress = real_time[0]
        stage._remaining -= 1

    calls = {"n": 0}

    def _fake_sleep(_s):
        calls["n"] += 1
        if calls["n"] == 1:
            _simulate_main_work()
        else:
            stage._done.set()   # end the loop on the next iteration

    monkeypatch.setattr(time, "sleep", _fake_sleep)
    monkeypatch.setattr(os, "_exit", lambda code: fired.append(code))

    stage._watchdog()
    assert fired == [], "watchdog must NOT fire while progress is happening"
