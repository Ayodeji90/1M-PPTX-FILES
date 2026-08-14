"""Batch/filename generator: padding, rollover, no gaps, no reuse."""
from __future__ import annotations

import pytest

from pptxsweeper.naming import (BatchAllocator, batch_folder_name,
                                batch_padding_width, delivered_filename,
                                manifest_filename)
from pptxsweeper.node import NodeIdentity


def test_padding_two_digits_until_99():
    assert batch_folder_name(1) == "BATCH_01"
    assert batch_folder_name(99) == "BATCH_99"
    assert batch_folder_name(100) == "BATCH_100"
    assert batch_folder_name(1000) == "BATCH_1000"


def test_delivered_filename_format():
    assert delivered_filename(4, 1, "pptx") == "BATCH_04_file_00001.pptx"
    assert delivered_filename(4, 99999, "pdf") == "BATCH_04_file_99999.pdf"
    assert delivered_filename(123, 7, "pdf") == "BATCH_123_file_00007.pdf"
    assert manifest_filename(4) == "BATCH_04_manifest.csv"


def test_only_final_formats_allowed():
    with pytest.raises(ValueError):
        delivered_filename(1, 1, "ppt")    # legacy ppt is never delivered
    with pytest.raises(ValueError):
        delivered_filename(1, 1, "docx")


def _mk_file(reg, sha):
    return reg.insert_file(sha256=sha, decision="DELIVER", quality="HIGH")


def test_sequential_names_no_gaps(registry):
    alloc = BatchAllocator(registry)
    batch = alloc.open_batch()
    names = [alloc.assign_filename(batch["batch_id"], _mk_file(registry, f"{i:064x}"), "pptx")
             for i in range(5)]
    assert names == [f"BATCH_01_file_{i:05d}.pptx" for i in range(1, 6)]


def test_assignment_idempotent_after_crash(registry):
    """Re-running assignment for the same file (crash replay) must return
    the same name and NOT advance the counter."""
    alloc = BatchAllocator(registry)
    batch = alloc.open_batch()
    fid = _mk_file(registry, "a" * 64)
    first = alloc.assign_filename(batch["batch_id"], fid, "pptx")
    replay = alloc.assign_filename(batch["batch_id"], fid, "pptx")
    assert first == replay == "BATCH_01_file_00001.pptx"
    fid2 = _mk_file(registry, "b" * 64)
    assert alloc.assign_filename(batch["batch_id"], fid2, "pdf") == "BATCH_01_file_00002.pdf"


def test_batch_ids_never_reused(registry):
    alloc = BatchAllocator(registry)
    b1 = alloc.open_batch()
    alloc.set_state(b1["batch_id"], "abandoned")
    b2 = alloc.open_batch()
    assert b2["batch_id"] == b1["batch_id"] + 1


def test_padding_monotonic_across_restart(registry):
    """Once any batch uses 3 digits, later batches never shrink to 2."""
    alloc = BatchAllocator(registry)
    with registry.tx():
        registry.conn.execute(
            "INSERT INTO batches (batch_id, folder_name, padding_width, state) "
            "VALUES (150, 'BATCH_150', 3, 'finalized')")
    nxt = alloc.open_batch()
    assert nxt["padding_width"] == 3
    assert nxt["folder_name"] == "BATCH_151"


def test_multinode_interleaved_batch_ids(registry, tmp_path):
    from pptxsweeper.db import Registry
    node0 = NodeIdentity(0, 3)
    node1 = NodeIdentity(1, 3)
    assert node0.first_batch_id() == 1
    assert node1.first_batch_id() == 2
    assert node0.next_batch_id(1) == 4   # 1,4,7,...
    assert node1.next_batch_id(2) == 5   # 2,5,8,...
    # allocator on node 1 skips ids owned by other nodes
    reg1 = Registry(tmp_path / "n1.db")
    alloc = BatchAllocator(reg1, node=node1)
    assert alloc.open_batch()["batch_id"] == 2
    reg1.close()


def test_standalone_node_namespace_id(monkeypatch):
    """A standalone node (NODE_COUNT=1) may use any NODE_ID >= 0: with a
    single node every owns_* check is True and node_id is purely a
    namespace (handoff CSV prefix, batch numbering) so two independent
    single-node machines never collide."""
    monkeypatch.setenv("NODE_ID", "1")
    monkeypatch.setenv("NODE_COUNT", "1")
    node = NodeIdentity.from_env()
    assert node.node_id == 1
    assert node.node_count == 1
    assert node.owns_domain("example.edu") is True
    assert node.owns_page(0) is True
    assert node.owns_batch_id(1) is True
    assert node.first_batch_id() == 2   # distinct from node0's 1

    # sanity: invalid combos still rejected
    monkeypatch.setenv("NODE_ID", "2")
    monkeypatch.setenv("NODE_COUNT", "2")
    with pytest.raises(ValueError):
        NodeIdentity.from_env()


def test_domain_sharding_partition():
    """Every domain is owned by exactly one node."""
    nodes = [NodeIdentity(i, 4) for i in range(4)]
    domains = [f"example{i}.com" for i in range(200)]
    for domain in domains:
        owners = [n.node_id for n in nodes if n.owns_domain(domain)]
        assert len(owners) == 1
