"""Multi-machine work splitting.

Every machine (laptop + each GCP VM) runs the same code against the same
Google Drive folder. Two env vars split the work with zero coordination:

    NODE_ID    - this machine's index (0-based)
    NODE_COUNT - total number of machines

Guarantees:
- A domain (and therefore a URL) is only ever processed by ONE machine:
  hash(domain) % NODE_COUNT == NODE_ID. Deterministic and stable across
  restarts (uses sha1, not Python's salted hash()).
- Batch numbers never collide across machines: node k of N creates
  batches k+1, k+1+N, k+1+2N, ... so collectively the batches form the
  full global sequence 1,2,3,... with no number ever reused.
- Cross-node file dedup (same file hosted on two different domains) is
  handled by the hash-exchange sync (`pptxsweeper sync-dedup`), which
  uploads this node's known hashes to Drive and imports the other
  nodes' lists.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class NodeIdentity:
    node_id: int
    node_count: int

    @classmethod
    def from_env(cls) -> "NodeIdentity":
        node_id = int(os.environ.get("NODE_ID", "0"))
        node_count = int(os.environ.get("NODE_COUNT", "1"))
        if node_count < 1:
            raise ValueError("NODE_COUNT must be >= 1")
        if node_id < 0:
            raise ValueError(f"NODE_ID must be >= 0, got {node_id}")
        if node_count > 1 and node_id >= node_count:
            raise ValueError(f"NODE_ID must be in [0, {node_count}), got {node_id}")
        return cls(node_id, node_count)

    # ------------------------------------------------------------------
    def owns_domain(self, domain: str) -> bool:
        """Stable shard check: does this machine own this domain?"""
        if self.node_count == 1:
            return True
        digest = hashlib.sha1(domain.lower().encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % self.node_count == self.node_id

    def filter_domains(self, domains: list[str]) -> list[str]:
        return [d for d in domains if self.owns_domain(d)]

    # ------------------------------------------------------------------
    # Discovery-work sharding
    #
    # Harvesters come in two shapes and must shard their *discovery work*
    # (which node calls which API) consistently with how the resulting
    # URLs are later owned for download:
    #
    #   * domain-list sources (Wayback, sitemaps, OAI repos, standards
    #     domain walks): iterate a domain list -> shard the LIST by domain
    #     with owns_domain(); ownership == domain, so a URL is discovered
    #     and downloaded by the same node.
    #   * global/aggregator sources (Brave, Zenodo, Figshare, OSF, IA,
    #     IETF, data.europa): paginated queries returning arbitrary or
    #     single-domain URLs -> shard the PAGES with owns_page(); the
    #     harvesting node owns whatever it discovered, so the download
    #     stage must NOT re-shard by domain (see download/worker.py).
    #
    # Because every node keeps its OWN local registry.db, sharding at
    # discovery time makes the N databases disjoint by construction and
    # eliminates the redundant N-times API hammering the old code did.
    # ------------------------------------------------------------------
    def owns_page(self, index: int) -> bool:
        """Round-robin page ownership for globally-paginated sources."""
        if self.node_count == 1:
            return True
        return index % self.node_count == self.node_id

    # ------------------------------------------------------------------
    def owns_batch_id(self, batch_id: int) -> bool:
        """Interleaved batch namespace: node k owns ids where (id-1) % N == k."""
        if self.node_count == 1:
            return True
        return (batch_id - 1) % self.node_count == self.node_id

    def first_batch_id(self) -> int:
        return self.node_id + 1

    def next_batch_id(self, max_existing: int) -> int:
        """Smallest id > max_existing owned by this node."""
        candidate = max_existing + 1
        while not self.owns_batch_id(candidate):
            candidate += 1
        return candidate
