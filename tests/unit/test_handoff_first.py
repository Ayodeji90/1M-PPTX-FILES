"""Tests for the handoff_first download gate.

A consumer node fed by another VM's handoff must keep its own harvested
URLs on standby: while ANY handoff-tagged row is still pending, the
downloader may only claim handoff rows. Once the handed-off queue is
drained, it falls back to claiming its own harvest.

Handoff rows are tagged metadata.handoff=true on import (stages/handoff.py)
so the gate works while the original discovery_source is preserved.
"""
from __future__ import annotations

import csv
import os

# Some suite-wide tests set PPTXSWEEPER_OVERRIDE; clear it so Config.load()
# in any downstream code resolves the base config only.
os.environ.pop("PPTXSWEEPER_OVERRIDE", None)


def _seed(reg, n: int, source: str, domain: str = "example.org",
          handoff: bool = False) -> None:
    meta = {"handoff": True} if handoff else {}
    reg.upsert_candidates([
        {"url": f"https://{domain}/{source}/{i}.pptx", "domain": domain,
         "tier": 4, "discovery_source": source, "metadata": meta}
        for i in range(n)
    ])


def _write_csv(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["url", "domain", "tier",
                                           "discovery_source", "metadata"])
        w.writeheader()
        w.writerows(rows)


def test_import_tags_handoff_rows(registry, tmp_path):
    """import_urls must tag rows metadata.handoff=true (used by the gate)."""
    csv_path = tmp_path / "handoff.csv"
    _write_csv(csv_path, [
        {"url": "https://a.org/1.pptx", "domain": "a.org", "tier": 1,
         "discovery_source": "wayback_cdx", "metadata": "{}"},
        {"url": "https://b.org/2.pptx", "domain": "b.org", "tier": 7,
         "discovery_source": "govdata:x", "metadata": "{}"},
    ])
    from pptxsweeper.stages.handoff import import_urls
    stats = import_urls(registry, csv_path)
    assert stats["new"] == 2
    row = registry.conn.execute(
        "SELECT discovery_source, metadata FROM urls WHERE url=?",
        ("https://a.org/1.pptx",)).fetchone()
    assert row["discovery_source"] == "wayback_cdx"   # preserved
    assert '"handoff": true' in row["metadata"]        # tagged


def test_handoff_first_positional_bool_claims_nothing(registry):
    """Regression: the download worker passed handoff_first as a positional
    arg, which landed in from_status (status=True matches no TEXT status)
    and silently claimed nothing. The gate must only be triggered via the
    keyword -- this documents why the call site uses named arguments."""
    _seed(registry, 5, "wayback_cdx", handoff=True)
    # positional bool in the from_status slot -> matches nothing
    assert registry.claim_urls(["example.org"], 10, True) == []
    # keyword -> gate engages and handoff rows are claimed
    claimed = registry.claim_urls(["example.org"], 10, handoff_first=True)
    assert len(claimed) == 5


def test_handoff_first_claims_only_handoff_while_pending(registry):
    _seed(registry, 5, "wayback_cdx", handoff=True)
    _seed(registry, 5, "ocw:mit.edu", domain="mit.edu")

    claimed = registry.claim_urls(["example.org", "mit.edu"], limit=20,
                                  handoff_first=True)
    sources = {r["discovery_source"] for r in claimed}
    assert sources == {"wayback_cdx"}, f"claimed non-handoff rows: {sources}"
    assert len(claimed) == 5


def test_handoff_first_falls_back_to_own_when_handoff_drained(registry):
    _seed(registry, 5, "wayback_cdx", handoff=True)
    _seed(registry, 5, "ocw:mit.edu", domain="mit.edu")

    # Drain the handoff queue first.
    first = registry.claim_urls(["example.org", "mit.edu"], limit=20,
                                handoff_first=True)
    assert len(first) == 5
    for row in first:
        registry.update_url(row["id"], status="downloaded")

    # Now the gate must allow this node's own harvest.
    second = registry.claim_urls(["example.org", "mit.edu"], limit=20,
                                 handoff_first=True)
    sources = {r["discovery_source"] for r in second}
    assert "ocw:mit.edu" in sources
    assert len(second) == 5


def test_handoff_first_noop_without_handoff_rows(registry):
    _seed(registry, 5, "ocw:mit.edu", domain="mit.edu")
    claimed = registry.claim_urls(["mit.edu"], limit=20, handoff_first=True)
    assert len(claimed) == 5


def test_claim_without_flag_unchanged(registry):
    """Default (handoff_first=False) keeps the old behavior: no gating."""
    _seed(registry, 3, "wayback_cdx", handoff=True)
    _seed(registry, 3, "ocw:mit.edu", domain="mit.edu")
    claimed = registry.claim_urls(["example.org", "mit.edu"], limit=20)
    assert len(claimed) == 6
    sources = {r["discovery_source"] for r in claimed}
    assert sources == {"wayback_cdx", "ocw:mit.edu"}


def test_handoff_first_blocks_wayback_until_handoff_drained(registry):
    _seed(registry, 2, "wayback_cdx", handoff=True)
    _seed(registry, 2, "wayback_cdx", domain="mit.edu")

    # While handoff rows are pending, other rows are NOT claimable.
    first = registry.claim_urls(["example.org", "mit.edu"], limit=10,
                                handoff_first=True)
    assert len(first) == 2
    for row in first:
        registry.update_url(row["id"], status="downloaded")

    # Handoff drained -> the rest become claimable.
    second = registry.claim_urls(["example.org", "mit.edu"], limit=10,
                                 handoff_first=True)
    assert len(second) == 2
