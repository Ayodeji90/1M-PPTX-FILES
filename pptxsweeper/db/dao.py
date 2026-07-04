"""Registry access layer.

Rules enforced here (see DECISIONS.md):
- WAL mode, busy_timeout, foreign keys on every connection.
- All writes inside explicit transactions; retry on SQLITE_BUSY with
  exponential backoff.
- Claim queries use UPDATE ... RETURNING so two concurrently running
  stage instances can never claim the same row.
"""
from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import apply_schema

_BUSY_RETRIES = 8
_BUSY_BASE_SLEEP = 0.01  # 10ms doubling, capped at 2s


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def connect(db_path: str | Path, apply: bool = True) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the async downloader executes DB ops via
    # asyncio.to_thread (pool threads), but they are strictly serialized
    # through the single writer task, so cross-thread use is safe.
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    if apply:
        apply_schema(conn)
    return conn


def with_busy_retry(fn, *args, **kwargs):
    """Run fn, retrying on SQLITE_BUSY/LOCKED with exponential backoff."""
    delay = _BUSY_BASE_SLEEP
    for attempt in range(_BUSY_RETRIES):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if attempt == _BUSY_RETRIES - 1:
                raise
            time.sleep(min(delay * (1 + random.random()), 2.0))
            delay *= 2


class Registry:
    """Thin DAO over the registry database. One instance per stage process."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.conn = connect(db_path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Registry":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------
    def tx(self):
        """Context manager for an IMMEDIATE transaction with busy-retry."""
        return _Tx(self.conn)

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------
    def upsert_candidates(self, candidates: Iterable[dict]) -> int:
        """Insert discovered URLs; ignore duplicates. Returns rows inserted."""
        rows = [
            (
                c["url"], c["domain"], c["tier"], c["discovery_source"],
                json.dumps(c.get("metadata") or {}),
            )
            for c in candidates
        ]
        if not rows:
            return 0

        def _do():
            with self.tx():
                before = self.conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
                self.conn.executemany(
                    "INSERT OR IGNORE INTO urls (url, domain, tier, discovery_source, metadata) "
                    "VALUES (?,?,?,?,?)",
                    rows,
                )
                after = self.conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
            return after - before

        return with_busy_retry(_do)

    def claim_urls(self, domains: Sequence[str], limit: int,
                   from_status: str = "discovered", to_status: str = "downloading") -> list[sqlite3.Row]:
        """Atomically claim up to `limit` URLs whose domain is in `domains`.

        Uses UPDATE ... RETURNING so concurrent claimers can't double-claim.
        Also picks up parked URLs whose parked_until has passed.
        """
        if not domains:
            return []
        placeholders = ",".join("?" for _ in domains)
        now = utcnow()

        def _do():
            with self.tx():
                return self.conn.execute(
                    f"""
                    UPDATE urls SET status=?, attempt_count=attempt_count+1, updated_at=?
                    WHERE id IN (
                        SELECT id FROM urls
                        WHERE domain IN ({placeholders})
                          AND (status=? OR (status='parked' AND parked_until IS NOT NULL AND parked_until <= ?))
                        -- live-origin URLs first: they parallelize across
                        -- hundreds of domains, while wayback-discovered
                        -- (dead-origin) URLs all funnel through the shared
                        -- web.archive.org fetcher (~3 req/s, throttled)
                        ORDER BY CASE WHEN discovery_source LIKE 'wayback%' THEN 1 ELSE 0 END, id
                        LIMIT ?
                    )
                    RETURNING *
                    """,
                    (to_status, now, *domains, from_status, now, limit),
                ).fetchall()

        return with_busy_retry(_do)

    def update_url(self, url_id: int, **fields: Any) -> None:
        self.update_urls([(url_id, fields)])

    def update_urls(self, updates: Sequence[tuple[int, dict[str, Any]]]) -> None:
        """Batch-update url rows. Each item: (url_id, {column: value})."""
        if not updates:
            return

        def _do():
            with self.tx():
                for url_id, fields in updates:
                    fields = dict(fields)
                    if "metadata" in fields and isinstance(fields["metadata"], dict):
                        fields["metadata"] = json.dumps(fields["metadata"])
                    fields["updated_at"] = utcnow()
                    cols = ", ".join(f"{k}=?" for k in fields)
                    self.conn.execute(
                        f"UPDATE urls SET {cols} WHERE id=?",
                        (*fields.values(), url_id),
                    )

        with_busy_retry(_do)

    def urls_by_status(self, status: str, limit: int | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM urls WHERE status=? ORDER BY id"
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.conn.execute(q, (status,)).fetchall()

    def distinct_domains(self, status: str = "discovered") -> list[str]:
        rows = self.conn.execute(
            """SELECT DISTINCT u.domain FROM urls u
               LEFT JOIN domains d ON d.domain = u.domain
               WHERE (u.status=? OR (u.status='parked' AND u.parked_until <= ?))
                 AND COALESCE(d.state,'active') != 'blacklisted'""",
            (status, utcnow()),
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Hash dedup
    # ------------------------------------------------------------------
    def hash_known(self, sha256: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM known_hashes WHERE sha256=? LIMIT 1", (sha256,)
            ).fetchone()
            is not None
        )

    def known_hash_origin(self, sha256: str) -> str | None:
        row = self.conn.execute(
            "SELECT origin FROM known_hashes WHERE sha256=? LIMIT 1", (sha256,)
        ).fetchone()
        return row[0] if row else None

    def other_url_with_hash(self, sha256: str, url_id: int) -> bool:
        """True if ANOTHER url row holds this hash in a live state.
        Distinguishes a real duplicate from a re-download of the same URL
        after a crash (whose hash is in known_hashes but whose status
        update was lost)."""
        return self.conn.execute(
            """SELECT 1 FROM urls WHERE sha256=? AND id != ?
               AND status IN ('downloaded','validated','classified','reserve',
                              'review','delivered') LIMIT 1""",
            (sha256, url_id),
        ).fetchone() is not None

    def add_known_hashes(self, hashes: Iterable[str], origin: str) -> int:
        rows = [(h.lower().strip(), origin) for h in hashes if h and len(h.strip()) == 64]

        def _do():
            with self.tx():
                before = self.conn.execute("SELECT COUNT(*) FROM known_hashes").fetchone()[0]
                self.conn.executemany(
                    "INSERT OR IGNORE INTO known_hashes (sha256, origin) VALUES (?,?)", rows
                )
                after = self.conn.execute("SELECT COUNT(*) FROM known_hashes").fetchone()[0]
            return after - before

        return with_busy_retry(_do) if rows else 0

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------
    def insert_file(self, **fields: Any) -> int:
        for k in ("feature_vectors", "quality_report", "compliance"):
            if k in fields and isinstance(fields[k], (dict, list)):
                fields[k] = json.dumps(fields[k])

        def _do():
            with self.tx():
                cols = ", ".join(fields)
                marks = ", ".join("?" for _ in fields)
                cur = self.conn.execute(
                    f"INSERT INTO files ({cols}) VALUES ({marks})", tuple(fields.values())
                )
                return cur.lastrowid

        return with_busy_retry(_do)

    def update_file(self, file_id: int, **fields: Any) -> None:
        for k in ("feature_vectors", "quality_report", "compliance"):
            if k in fields and isinstance(fields[k], (dict, list)):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = utcnow()

        def _do():
            with self.tx():
                cols = ", ".join(f"{k}=?" for k in fields)
                self.conn.execute(f"UPDATE files SET {cols} WHERE id=?", (*fields.values(), file_id))

        with_busy_retry(_do)

    def file_by_sha256(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE sha256=?", (sha256,)).fetchone()

    # ------------------------------------------------------------------
    # Domains
    # ------------------------------------------------------------------
    def get_domain(self, domain: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM domains WHERE domain=?", (domain,)).fetchone()

    def upsert_domain(self, domain: str, **fields: Any) -> None:
        def _do():
            with self.tx():
                self.conn.execute(
                    "INSERT OR IGNORE INTO domains (domain) VALUES (?)", (domain,)
                )
                if fields:
                    fields["updated_at"] = utcnow()
                    cols = ", ".join(f"{k}=?" for k in fields)
                    self.conn.execute(
                        f"UPDATE domains SET {cols} WHERE domain=?", (*fields.values(), domain)
                    )

        with_busy_retry(_do)

    # ------------------------------------------------------------------
    # Budget + events
    # ------------------------------------------------------------------
    def budget_used_today(self) -> int:
        row = self.conn.execute(
            "SELECT bytes_uploaded FROM upload_budget WHERE day=?", (today_utc(),)
        ).fetchone()
        return row[0] if row else 0

    def budget_add(self, nbytes: int) -> None:
        def _do():
            with self.tx():
                self.conn.execute(
                    """INSERT INTO upload_budget (day, bytes_uploaded) VALUES (?,?)
                       ON CONFLICT(day) DO UPDATE SET bytes_uploaded=bytes_uploaded+excluded.bytes_uploaded""",
                    (today_utc(), nbytes),
                )

        with_busy_retry(_do)

    def log_event(self, kind: str, domain: str | None = None, detail: str | None = None) -> None:
        def _do():
            with self.tx():
                self.conn.execute(
                    "INSERT INTO events (kind, domain, detail) VALUES (?,?,?)",
                    (kind, domain, detail),
                )

        with_busy_retry(_do)


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK context manager."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        # isolation_level=None means autocommit; take the write lock explicitly.
        # Nested use (a tx inside a tx) piggybacks on the outer transaction.
        self._owns = not self.conn.in_transaction
        if self._owns:
            with_busy_retry(self.conn.execute, "BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._owns:
            return False
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
