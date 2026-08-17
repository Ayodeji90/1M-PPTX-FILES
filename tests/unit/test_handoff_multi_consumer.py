"""Tests for the multi-consumer handoff split.

With several consumer machines (VM2 = node1, VM3 = node2, ...) the
producer writes ONE CSV per consumer, named node{consumer_id}_{ts}.csv,
and partitions the URL space by `bucket(url) % n_consumers == node_id`
so every URL is handed to exactly one machine -- no double-downloads,
no consumer racing another for the same CSV.
"""
from __future__ import annotations

import os

# Some suite-wide tests set PPTXSWEEPER_OVERRIDE; clear it so Config.load()
# in any downstream code resolves the base config only.
os.environ.pop("PPTXSWEEPER_OVERRIDE", None)


def _seed(reg, n: int, domain: str = "example.org") -> None:
    reg.upsert_candidates([
        {"url": f"https://{domain}/{i}.pptx", "domain": domain,
         "tier": 4, "discovery_source": "wayback_cdx", "metadata": {}}
        for i in range(n)
    ])


def test_multi_consumer_export_is_disjoint_and_total(registry, tmp_path):
    """fraction=1.0 with 2 consumers: every URL goes to exactly one CSV."""
    _seed(registry, 400)
    from pptxsweeper.stages.handoff import export_urls

    p0 = tmp_path / "node1_x.csv"
    p1 = tmp_path / "node2_x.csv"
    s0 = export_urls(registry, fraction=1.0, out_path=p0, node_id=0, n_consumers=2)
    s1 = export_urls(registry, fraction=1.0, out_path=p1, node_id=1, n_consumers=2)

    def urls(p):
        return {r["url"] for r in __import__("csv").DictReader(open(p))}

    u0, u1 = urls(p0), urls(p1)
    assert s0["exported"] + s1["exported"] == 400
    assert u0.isdisjoint(u1), "consumers must never receive the same URL"
    assert len(u0) + len(u1) == 400, "every URL must be handed to exactly one consumer"


def test_multi_consumer_partition_stable_across_exports(registry, tmp_path):
    """The same URL must always land on the same consumer (stable hash).

    Exports are re-run on a fresh pool (same URLs re-seeded) and must
    produce identical partitions."""
    import csv
    def urls(p):
        return {r["url"] for r in csv.DictReader(open(p))}

    from pptxsweeper.stages.handoff import export_urls

    _seed(registry, 200)
    p0a = tmp_path / "a1.csv"
    p1a = tmp_path / "b1.csv"
    s0a = export_urls(registry, fraction=1.0, out_path=p0a, node_id=0, n_consumers=2)
    s1a = export_urls(registry, fraction=1.0, out_path=p1a, node_id=1, n_consumers=2)
    assert s0a["exported"] + s1a["exported"] == 200

    # Same URL pool again: restore the rows to 'discovered' (the export
    # marked them filtered_out) and re-export -- the same URLs must
    # partition identically (stable hash).
    registry.conn.execute("UPDATE urls SET status='discovered'")
    p0b = tmp_path / "a2.csv"
    p1b = tmp_path / "b2.csv"
    export_urls(registry, fraction=1.0, out_path=p0b, node_id=0, n_consumers=2)
    export_urls(registry, fraction=1.0, out_path=p1b, node_id=1, n_consumers=2)

    assert urls(p0a) == urls(p0b)
    assert urls(p1a) == urls(p1b)


def test_multi_consumer_respects_fraction(registry, tmp_path):
    """fraction<1.0 caps the total handed off; partition still disjoint."""
    _seed(registry, 500)
    from pptxsweeper.stages.handoff import export_urls

    s0 = export_urls(registry, fraction=0.6, out_path=tmp_path / "c1.csv",
                     node_id=0, n_consumers=2)
    s1 = export_urls(registry, fraction=0.6, out_path=tmp_path / "c2.csv",
                     node_id=1, n_consumers=2)
    assert s0["exported"] + s1["exported"] < 500
    # sanity: the split is still roughly even between the two consumers
    assert abs(s0["exported"] - s1["exported"]) <= max(10, (s0["exported"] + s1["exported"]) // 10)


def test_single_consumer_backward_compatible(registry, tmp_path):
    """n_consumers=1 behaves like the legacy export (no partition filter)."""
    _seed(registry, 50)
    from pptxsweeper.stages.handoff import export_urls

    s = export_urls(registry, fraction=1.0, out_path=tmp_path / "legacy.csv",
                    node_id=0, n_consumers=1)
    assert s["exported"] == 50
