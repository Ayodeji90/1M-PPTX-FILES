"""Regression tests for classify resilience.

The production bug this locks in: a single pathological payload could
hang a classify worker forever (blocked on pipe/futex), and since the
parent wait()s on that future, the ENTIRE deliver chain (classify ->
package -> Drive) wedged -- uploads stopped while downloads kept
flowing. The per-file SIGALRM timeout turns a hung payload into a
terminal reject instead.
"""
from __future__ import annotations

import signal

from pptxsweeper.stages.classify_stage import _compute_task


class _FakeReport:
    quality = "HIGH"
    decision = "DELIVER"
    slide_count = 10
    full_text = "investor presentation results growth strategy"
    explanations = ["good"]

    def to_dict(self) -> dict:
        return {"doc_properties": {}}

    def feature_vectors_json(self) -> str:
        return "{}"


class _FakeScreens:
    forces_reject = False
    forces_review = False
    details = []

    def to_dict(self) -> dict:
        return {}


def _task(decks, file_timeout_s: int = 0, payload_key: str = "chart_heavy") -> dict:
    return {
        "url_id": 7,
        "payload": str(decks[payload_key]),
        "allowed_formats": ("pptx", "ppt"),
        "tmp_dir": str(decks[payload_key].parent),
        "soffice_bin": "soffice",
        "conv_timeout": 30,
        "thresholds": {},
        "image_thresholds": {},
        "ocr": False,
        "robots_status": None,
        "file_timeout_s": file_timeout_s,
    }


def _patch_screens(monkeypatch):
    """Make the compliance screens call deterministic and inert."""
    monkeypatch.setattr(
        "pptxsweeper.compliance.screens.run_screens",
        lambda *_a, **_k: _FakeScreens())


def test_hung_payload_becomes_reject_not_wedge(decks, monkeypatch):
    """THE regression: quality classify hangs -> timeout -> terminal reject."""
    import time

    def _hang(*_a, **_k):
        time.sleep(60)          # would hang the worker forever without alarm
        raise AssertionError("never reached")

    _patch_screens(monkeypatch)
    monkeypatch.setattr("pptxsweeper.quality.classify", _hang)
    out = _compute_task(_task(decks, file_timeout_s=2))
    assert out["reject"] == "classify_timeout"
    assert out["format"] is None
    assert out["converted"] == 0


def test_no_timeout_configured_means_no_alarm(decks, monkeypatch):
    """file_timeout_s=0 (disabled) must not touch signal handlers."""
    calls = []

    def _fast(*_a, **_k):
        calls.append(1)
        return _FakeReport()

    _patch_screens(monkeypatch)
    monkeypatch.setattr("pptxsweeper.quality.classify", _fast)
    out = _compute_task(_task(decks, file_timeout_s=0))
    assert out["reject"] is None
    assert out["decision"] == "DELIVER"
    assert calls == [1]


def test_alarm_restored_after_success(decks, monkeypatch):
    """The SIGALRM handler must be restored so later files run clean."""
    _patch_screens(monkeypatch)
    monkeypatch.setattr("pptxsweeper.quality.classify",
                        lambda *_a, **_k: _FakeReport())
    before = signal.getsignal(signal.SIGALRM)
    _compute_task(_task(decks, file_timeout_s=5))
    after = signal.getsignal(signal.SIGALRM)
    # restored to the caller's handler, not left as our timeout handler
    assert after is before


def test_valid_payload_still_delivers_with_timeout_on(decks, monkeypatch):
    """Timeouts enabled must not break the happy path."""
    _patch_screens(monkeypatch)
    monkeypatch.setattr("pptxsweeper.quality.classify",
                        lambda *_a, **_k: _FakeReport())
    out = _compute_task(_task(decks, file_timeout_s=30))
    assert out["reject"] is None
    assert out["decision"] == "DELIVER"


def test_run_workers_pool_path_end_to_end(tmp_path, decks):
    """THE regression for the production crash: the ProcessPoolExecutor
    branch of `_run_workers` NameError'd because the futures imports were
    (wrongly) scoped inside `run()` -- so EVERY classify pass crashed at
    the pool line, classify never printed "done", and the deliver chain
    (classify -> package -> Drive) froze while downloads kept flowing.
    This drives the REAL pool path with real decks and a real registry:
    chart_heavy must DELIVER, text_heavy must REJECT, zero errors.
    """
    import hashlib
    import json
    import shutil

    from pptxsweeper.config import Config
    from pptxsweeper.db.dao import Registry
    from pptxsweeper.stages.classify_stage import ClassifyStage

    cfg = Config.load()
    cfg.raw["paths"]["data_dir"] = str(tmp_path)
    cfg.raw["paths"]["download_tmp_dir"] = str(tmp_path / "tmp")
    cfg.raw["paths"]["staging_dir"] = str(tmp_path / "staging")
    cfg.raw["paths"]["review_dir"] = str(tmp_path / "review")
    cfg.raw["classify"]["workers"] = 2   # force the multi-process pool path
    cfg.raw["quality"]["use_ocr"] = False

    reg = Registry(tmp_path / "registry.db")
    urls = ["https://example.com/chart.pptx", "https://example.com/text.pptx"]
    for idx, (url, key) in enumerate(zip(urls, ("chart_heavy", "text_heavy"))):
        dst = tmp_path / f"url{idx}.pptx"
        shutil.copyfile(decks[key], dst)
        sha = hashlib.sha256(dst.read_bytes()).hexdigest()
        with reg.tx():
            reg.conn.execute(
                "INSERT INTO urls (url, domain, tier, discovery_source, status, "
                "sha256, metadata) VALUES (?,?,?,?,?,?,?)",
                (url, "example.com", 1, "test", "downloaded", sha,
                 json.dumps({"local_path": str(dst)})))

    stage = ClassifyStage(cfg, reg)
    stats = stage.run()
    assert stats["errors"] == 0, stats
    assert stats["deliver"] == 1, stats   # chart_heavy -> staging
    assert stats["reject"] == 1, stats    # text_heavy -> payload deleted
    assert len(list((tmp_path / "staging").glob("*.pptx"))) == 1
    statuses = {r["url"]: r["status"]
                for r in reg.conn.execute("SELECT url, status FROM urls")}
    assert statuses[urls[0]] == "classified"
    assert statuses[urls[1]] == "rejected"
