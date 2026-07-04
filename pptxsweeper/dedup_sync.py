"""Cross-machine duplicate prevention via Google Drive hash exchange.

Domain sharding (node.py) already guarantees no two machines download
the same URL. This module closes the remaining gap -- the same file
hosted on two different websites -- by exchanging SHA256 lists through
a `_dedup/` folder on Drive:

    _dedup/node_0_hashes.txt.gz
    _dedup/node_1_hashes.txt.gz
    ...

Each machine periodically uploads its own known-hash list and imports
every other machine's list into its local registry. A hash seen in any
list is never downloaded again anywhere. Lists are append-only exports,
so concurrent access through Drive is safe (each node only ever writes
its own file).
"""
from __future__ import annotations

import gzip
import logging
from pathlib import Path

from .db.dao import Registry
from .node import NodeIdentity
from .packager.rclone import Rclone, RcloneError

log = logging.getLogger("pptxsweeper.dedup_sync")


def _own_list_name(node: NodeIdentity) -> str:
    return f"node_{node.node_id}_hashes.txt.gz"


def export_own_hashes(reg: Registry, node: NodeIdentity, tmp_dir: Path) -> Path:
    rows = reg.conn.execute("SELECT sha256 FROM known_hashes ORDER BY sha256").fetchall()
    out = tmp_dir / _own_list_name(node)
    with gzip.open(out, "wt", encoding="ascii") as fh:
        for row in rows:
            fh.write(row[0] + "\n")
    return out


def sync(reg: Registry, node: NodeIdentity, rclone: Rclone, tmp_dir: Path,
         dedup_folder: str = "_dedup") -> dict:
    """Upload own hash list; import peers' lists. Returns summary counts."""
    tmp_dir.mkdir(parents=True, exist_ok=True)

    own = export_own_hashes(reg, node, tmp_dir)
    rclone.mkdir(dedup_folder)
    rclone.copy_file(own, dedup_folder)
    uploaded = own.stat().st_size

    imported = 0
    for entry in rclone.lsjson(dedup_folder):
        name = entry.get("Name", "")
        if not name.endswith("_hashes.txt.gz") or name == _own_list_name(node):
            continue
        try:
            rclone.download_file((dedup_folder, name), tmp_dir)
        except RcloneError:
            log.warning("could not download peer hash list %s; skipping", name)
            continue
        peer_file = tmp_dir / name
        with gzip.open(peer_file, "rt", encoding="ascii") as fh:
            hashes = [line.strip() for line in fh if line.strip()]
        imported += reg.add_known_hashes(hashes, origin=f"peer:{name}")
        peer_file.unlink(missing_ok=True)

    log.info("dedup sync done: uploaded own list (%d bytes), imported %d new peer hashes",
             uploaded, imported)
    return {"uploaded_bytes": uploaded, "imported_new": imported}
