"""Tests for the handoff_first download gate.

A consumer node fed by another VM's handoff must keep its own harvested
URLs on standby: while ANY handoff-discovered row is still pending, the
downloader may only claim handoff rows. Once the handed-off queue is
drained, it falls back to claiming its own harvest.
"""
from __future__ import annotations

import os

# Some suite-wide tests set PPTXSWEEPER_OVERRIDE; clear it so Config.load()
# in any downstream code resolves the base config only.
os.environ.pop("PPTXSWEEPER_OVERRIDE", None)


def _seed(reg, n: int, source: str, domain: str = "example.org") -> None:
    reg.upsert_candidates([
        {"url": f"https://{domain}/{source}/{i}.pptx", "domain": domain,
         "tier": 4, "discovery_source": source, "metadata": {}}
        for i in range(n)
    ])


def test_handoff_first_claims_only_handoff_while_pending(registry):
    _seed(registry, 5, "handoff")
    _seed(registry, 5, "ocw:mit.edu", domain="mit.edu")

    claimed = registry.claim_urls(["example.org", "mit.edu"], limit=20,
                                  handoff_first=True)
    sources = {r["discovery_source"] for r in claimed}
    assert sources == {"handoff"}, f"claimed non-handoff rows: {sources}"
    assert len(claimed) == 5


def test_handoff_first_falls_back_to_own_when_handoff_drained(registry):
    _seed(registry, 5, "handoff")
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
    _seed(registry, 3, "handoff")
    _seed(registry, 3, "ocw:mit.edu", domain="mit.edu")
    claimed = registry.claim_urls(["example.org", "mit.edu"], limit=20)
    assert len(claimed) == 6
    sources = {r["discovery_source"] for r in claimed}
    assert sources == {"handoff", "ocw:mit.edu"}


def test_handoff_first_blocks_wayback_until_handoff_drained(registry):
    _seed(registry, 2, "wayback_cdx")
    _seed(registry, 2, "handoff")

    # While handoff rows are pending, wayback rows are NOT claimable.
    first = registry.claim_urls(["example.org"], limit=10, handoff_first=True)
    assert [r["discovery_source"] for r in first] == ["handoff", "handoff"]
    for row in first:
        registry.update_url(row["id"], status="downloaded")

    # Handoff drained -> wayback rows become claimable.
    second = registry.claim_urls(["example.org"], limit=10, handoff_first=True)
    assert [r["discovery_source"] for r in second] == ["wayback_cdx", "wayback_cdx"]
