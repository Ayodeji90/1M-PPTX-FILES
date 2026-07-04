"""Batch and delivered-filename generation.

Contract (client acceptance criteria):
- `BATCH_{NN}_file_{NNNNN}.{ext}`; batch zero-padded to 2 digits, growing
  to 3 automatically past BATCH_99 (monotonic: once any batch uses 3
  digits, no later batch uses 2).
- File counter zero-padded to 5 digits, sequential within the batch from
  00001, no gaps in a finalized batch, never reused after a crash.
- Batch numbers globally sequential, never reused.

All state lives on the `batches` row (padding_width, next_file_counter)
and every assignment happens in one transaction, so a crash between
"counter incremented" and "file row written" is impossible.
"""
from __future__ import annotations

import sqlite3

from .db.dao import Registry, utcnow, with_busy_retry
from .node import NodeIdentity

MIN_PADDING = 2
FILE_PADDING = 5


def batch_padding_width(batch_id: int, min_padding: int = MIN_PADDING) -> int:
    """Digits used for the batch number: 2 until 99, then grows (100 -> 3)."""
    return max(min_padding, len(str(batch_id)))


def batch_folder_name(batch_id: int, padding_width: int | None = None) -> str:
    width = padding_width or batch_padding_width(batch_id)
    return f"BATCH_{batch_id:0{width}d}"


def delivered_filename(batch_id: int, counter: int, ext: str, padding_width: int | None = None) -> str:
    if ext.startswith("."):
        ext = ext[1:]
    if ext not in ("pptx", "pdf"):
        raise ValueError(f"delivered extension must be pptx or pdf, got {ext!r}")
    if counter < 1 or counter > 10 ** FILE_PADDING - 1:
        raise ValueError(f"file counter out of range: {counter}")
    width = padding_width or batch_padding_width(batch_id)
    return f"BATCH_{batch_id:0{width}d}_file_{counter:0{FILE_PADDING}d}.{ext}"


def manifest_filename(batch_id: int, padding_width: int | None = None) -> str:
    width = padding_width or batch_padding_width(batch_id)
    return f"BATCH_{batch_id:0{width}d}_manifest.csv"


class BatchAllocator:
    """Transactional batch + filename allocation against the registry."""

    def __init__(self, reg: Registry, min_padding: int = MIN_PADDING,
                 node: NodeIdentity | None = None):
        self.reg = reg
        self.min_padding = min_padding
        # Multi-machine runs interleave batch ids (node k of N creates
        # k+1, k+1+N, ...) so numbers are globally unique with no
        # coordination. Single machine: plain MAX+1.
        self.node = node or NodeIdentity(0, 1)

    # ------------------------------------------------------------------
    def open_batch(self) -> sqlite3.Row:
        """Return the currently open batch, creating the next one if none.

        Batch ids are MAX(batch_id)+1 — never reused, even for abandoned
        batches. Padding width is pinned on the row at creation and is
        monotonic: it can never be smaller than the widest previous batch.
        """
        def _do():
            with self.reg.tx():
                row = self.reg.conn.execute(
                    "SELECT * FROM batches WHERE state IN ('open','packing') "
                    "ORDER BY batch_id LIMIT 1"
                ).fetchone()
                if row:
                    return row
                cur = self.reg.conn.execute(
                    "SELECT COALESCE(MAX(batch_id), 0), COALESCE(MAX(padding_width), ?) FROM batches",
                    (self.min_padding,),
                )
                max_id, max_width = cur.fetchone()
                batch_id = self.node.next_batch_id(max_id)
                width = max(max_width, batch_padding_width(batch_id, self.min_padding))
                folder = batch_folder_name(batch_id, width)
                self.reg.conn.execute(
                    "INSERT INTO batches (batch_id, folder_name, padding_width) VALUES (?,?,?)",
                    (batch_id, folder, width),
                )
                return self.reg.conn.execute(
                    "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
                ).fetchone()

        return with_busy_retry(_do)

    # ------------------------------------------------------------------
    def assign_filename(self, batch_id: int, file_id: int, ext: str) -> str:
        """Assign the next sequential name in `batch_id` to files.id=file_id.

        Idempotent: if the file already has a delivered_filename in this
        batch (crash after assignment), that name is returned unchanged —
        the counter is not advanced, so no gaps and no reuse.
        """
        def _do():
            with self.reg.tx():
                existing = self.reg.conn.execute(
                    "SELECT delivered_filename FROM files WHERE id=? AND batch_id=?",
                    (file_id, batch_id),
                ).fetchone()
                if existing and existing[0]:
                    return existing[0]

                batch = self.reg.conn.execute(
                    "SELECT padding_width, next_file_counter, state FROM batches WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if batch is None:
                    raise ValueError(f"batch {batch_id} does not exist")
                if batch["state"] not in ("open", "packing"):
                    raise ValueError(f"batch {batch_id} is {batch['state']}; cannot assign names")

                counter = batch["next_file_counter"]
                name = delivered_filename(batch_id, counter, ext, batch["padding_width"])
                self.reg.conn.execute(
                    "UPDATE batches SET next_file_counter=? WHERE batch_id=?",
                    (counter + 1, batch_id),
                )
                self.reg.conn.execute(
                    "UPDATE files SET delivered_filename=?, batch_id=?, updated_at=? WHERE id=?",
                    (name, batch_id, utcnow(), file_id),
                )
                return name

        return with_busy_retry(_do)

    # ------------------------------------------------------------------
    def set_state(self, batch_id: int, state: str, **fields) -> None:
        def _do():
            with self.reg.tx():
                fields["state"] = state
                cols = ", ".join(f"{k}=?" for k in fields)
                self.reg.conn.execute(
                    f"UPDATE batches SET {cols} WHERE batch_id=?",
                    (*fields.values(), batch_id),
                )

        with_busy_retry(_do)
