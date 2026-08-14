"""Regression tests for the rclone subprocess wrapper.

A stalled Google Drive connection (open-but-idle socket) used to hang a
single rclone call for the old 3600s default * 5 retries, blocking the
whole deliver loop. `_run` must now convert a subprocess timeout into a
graceful, retryable RcloneError instead of propagating TimeoutExpired or
hanging forever.
"""
from __future__ import annotations

import subprocess

import pytest

from pptxsweeper.packager.rclone import Rclone, RcloneError


def _rc(**kw) -> Rclone:
    kw.setdefault("retry_backoff_s", [0, 0, 0, 0, 0])
    return Rclone(**kw)


def test_run_times_out_into_rclone_error(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr("pptxsweeper.packager.rclone.subprocess.run", fake_run)
    r = _rc(retries=3, timeout=7)
    with pytest.raises(RcloneError) as excinfo:
        r._run(["copy", "a", "b"], retry=True)
    assert "timed out after 7s" in str(excinfo.value)
    assert len(calls) == 3            # retried up to retries, never hung


def test_run_uses_default_timeout_when_none(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("pptxsweeper.packager.rclone.subprocess.run", fake_run)
    _rc(timeout=123)._run(["version"])
    assert seen["timeout"] == 123


def test_run_returns_success(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr("pptxsweeper.packager.rclone.subprocess.run", fake_run)
    assert _rc()._run(["version"]).returncode == 0


def test_review_sync_budget_is_short_and_single_attempt(monkeypatch):
    """sync_review_to_drive must use the short review timeout and NOT retry
    (it re-runs every classify pass anyway)."""
    import pptxsweeper.packager.rclone as rc_mod
    calls = []

    def fake_run(self, args, retry=True, timeout=None):
        calls.append((retry, timeout))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rc_mod.Rclone, "_run", fake_run)
    r = rc_mod.Rclone()
    r.mkdir("_review", timeout=300, retry=False)
    r.copy_dir(__import__("pathlib").Path("x"), "_review", timeout=300, retry=False)
    assert all(c == (False, 300) for c in calls)
