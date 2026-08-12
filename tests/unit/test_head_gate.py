"""Regression tests for the downloader's HEAD gate.

The production bug this locks in: figshare (and other S3-presigned)
download URLs are signed for GET only, so a HEAD request always returns
403 while the GET succeeds. The gate used to treat any RETRYABLE_STATUS
(which included 403) as \"retry\", so those URLs were retried 4x, marked
dead, and never GET'd. A HEAD 403 must fall through and let GET decide.
"""
from __future__ import annotations

from pptxsweeper.download.worker import _head_verdict

MAX = 300 * 1024 * 1024   # 300 MB
MIN = 10 * 1024           # 10 KB


def _v(status: int, ctype: str | None = None, clen: str | None = None) -> str:
    return _head_verdict(status, ctype, clen, MAX, MIN)


# --- the production bug: 403 must NOT retry ---------------------------
def test_403_falls_through_to_get():
    """HEAD 403 (GET-signed presigned URL) -> 'ok', so GET decides."""
    assert _v(403) == "ok"


def test_403_with_html_ctype_still_ok():
    assert _v(403, "text/html") == "ok"


# --- genuinely transient statuses DO retry ----------------------------
def test_429_retries():
    assert _v(429) == "retry"


def test_5xx_retries():
    assert _v(500) == "retry"
    assert _v(502) == "retry"
    assert _v(503) == "retry"


# --- other definitive statuses fall through to GET --------------------
def test_404_falls_through():
    assert _v(404) == "ok"


def test_401_falls_through():
    assert _v(401) == "ok"


# --- successful HEAD may gate on content ------------------------------
def test_html_skip():
    assert _v(200, "text/html; charset=utf-8") == "skip_html"


def test_oversize_skip():
    assert _v(200, "application/octet-stream", str(MAX + 1)) == "skip_size"


def test_undersize_skip():
    assert _v(200, "application/octet-stream", str(MIN - 1)) == "skip_size"


def test_doc_content_type_ok():
    assert _v(200, "application/vnd.openxmlformats-officedocument."
                   "presentationml.presentation", str(50 * 1024)) == "ok"


def test_missing_ctype_or_clen_ok():
    assert _v(200) == "ok"
    assert _v(200, None, None) == "ok"


def test_non_numeric_clen_ok():
    assert _v(200, "application/pdf", "chunked") == "ok"
