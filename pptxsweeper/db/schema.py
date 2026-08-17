"""Registry schema: DDL + forward-only migrations.

Single SQLite database in WAL mode. Every stage is idempotent and
resumable; a killed process must never corrupt state, so all DDL runs
inside a transaction and schema_version is bumped last.
"""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

# NOTE on statuses (urls.status):
#   discovered    - harvester wrote the candidate
#   filtered_out  - pre-download filter excluded it (blocklist/cap/extension)
#   head_checked  - HEAD passed content-type/length sanity
#   downloading   - claimed by a download worker
#   downloaded    - payload on local disk, sha256 computed
#   duplicate     - sha256 already in registry; payload dropped
#   validated     - magic bytes + full open validation passed
#   classified    - quality engine ran (decision on files row)
#   reserve       - DELIVER-quality surplus MEDIUM held for a later batch
#   review        - needs human triage (borderline quality / PII / minors)
#   delivered     - uploaded to Drive and verified
#   rejected      - terminal reject (validation/quality/compliance/conversion)
#   dead          - origin unreachable and no wayback copy
#   parked        - domain circuit-breaker parked; retry after parked_until
#   blacklisted   - domain permanently blacklisted

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS urls (
    id               INTEGER PRIMARY KEY,
    url              TEXT NOT NULL UNIQUE,
    domain           TEXT NOT NULL,
    tier             INTEGER NOT NULL,
    discovery_source TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'discovered'
        CHECK (status IN ('discovered','filtered_out','head_checked','downloading',
                          'downloaded','duplicate','validated','classified','reserve',
                          'review','delivered','rejected','dead','parked','blacklisted')),
    sha256           TEXT,
    content_length   INTEGER,
    http_status      INTEGER,
    robots_status    TEXT,               -- allowed | disallowed | unavailable
    retrieval_method TEXT DEFAULT 'origin' CHECK (retrieval_method IN ('origin','wayback')),
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    parked_until     TEXT,
    batch_id         INTEGER,
    reject_reason    TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    metadata         TEXT NOT NULL DEFAULT '{}'   -- JSON
);
CREATE INDEX IF NOT EXISTS idx_urls_status          ON urls(status);
CREATE INDEX IF NOT EXISTS idx_urls_domain          ON urls(domain);
CREATE INDEX IF NOT EXISTS idx_urls_status_domain   ON urls(status, domain);
CREATE INDEX IF NOT EXISTS idx_urls_sha256          ON urls(sha256);
CREATE INDEX IF NOT EXISTS idx_urls_tier_status     ON urls(tier, status);

CREATE TABLE IF NOT EXISTS files (
    id                 INTEGER PRIMARY KEY,
    url_id             INTEGER REFERENCES urls(id),
    sha256             TEXT NOT NULL UNIQUE,
    original_filename  TEXT,
    delivered_filename TEXT UNIQUE,
    local_path         TEXT,              -- current payload location (staging/review); NULL after delivery/cleanup
    format             TEXT CHECK (format IN ('pptx','pdf','ppt')),
    file_size          INTEGER,
    slide_count        INTEGER,
    quality            TEXT CHECK (quality IN ('HIGH','MEDIUM','LOW')),
    decision           TEXT CHECK (decision IN ('DELIVER','REVIEW','REJECT')),
    feature_vectors    TEXT,              -- JSON: per-slide vectors
    quality_report     TEXT,              -- JSON: signals + explanations
    compliance         TEXT,              -- JSON: per-screen outcomes
    converted_from_ppt INTEGER NOT NULL DEFAULT 0,
    delivered_at       TEXT,
    batch_id           INTEGER,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_files_batch    ON files(batch_id);
CREATE INDEX IF NOT EXISTS idx_files_decision ON files(decision, quality);
CREATE INDEX IF NOT EXISTS idx_files_url      ON files(url_id);

CREATE TABLE IF NOT EXISTS domains (
    domain            TEXT PRIMARY KEY,
    robots_cache      TEXT,
    robots_fetched_at TEXT,
    crawl_delay       REAL,
    state             TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','parked','blacklisted')),
    failure_streak    INTEGER NOT NULL DEFAULT 0,
    park_count        INTEGER NOT NULL DEFAULT 0,
    parked_until      TEXT,
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id          INTEGER PRIMARY KEY,     -- global sequential, never reused
    folder_name       TEXT NOT NULL UNIQUE,    -- e.g. BATCH_04
    padding_width     INTEGER NOT NULL,        -- pinned at creation; monotonic across batches
    state             TEXT NOT NULL DEFAULT 'open'
        CHECK (state IN ('open','packing','uploading','finalized','abandoned')),
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finalized_at      TEXT,
    file_count        INTEGER NOT NULL DEFAULT 0,
    high_count        INTEGER NOT NULL DEFAULT 0,
    medium_count      INTEGER NOT NULL DEFAULT 0,
    composition_ok    INTEGER,
    uploaded_at       TEXT,
    drive_path        TEXT,
    manifest_sha256   TEXT,
    next_file_counter INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id                   INTEGER PRIMARY KEY,
    file_id              INTEGER REFERENCES files(id),
    batch_id             INTEGER,
    delivered_filename   TEXT,
    sha256               TEXT,
    source_url           TEXT,
    source_domain        TEXT,
    download_url         TEXT,
    original_filename    TEXT,
    format               TEXT,
    converted_from_ppt   INTEGER,
    slide_count          INTEGER,
    quality_class        TEXT,
    collection_ts        TEXT,      -- when the URL was discovered
    download_ts          TEXT,
    http_status          INTEGER,
    robots_status        TEXT,
    retrieval_method     TEXT,
    public_access_status TEXT,      -- reachable | dead | archived-via-wayback
    screen_pirate        TEXT,
    screen_robots        TEXT,
    screen_rights        TEXT,
    screen_pii           TEXT,
    screen_minors        TEXT,
    screen_prohibited    TEXT,
    -- image delivery: which page of which source file, its own hashes
    page_index           INTEGER,
    image_sha256         TEXT,
    phash                TEXT,
    source_file_sha256   TEXT,
    extraction_method    TEXT,
    final_status         TEXT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_log(batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_file  ON audit_log(file_id);

-- Known hashes from prior collections (catalog import) and all accepted
-- files: consulted during download to drop duplicates immediately.
CREATE TABLE IF NOT EXISTS known_hashes (
    sha256     TEXT PRIMARY KEY,
    origin     TEXT NOT NULL,       -- catalog_import | pipeline
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Daily upload byte budget accounting (Google 750GB/day cap).
CREATE TABLE IF NOT EXISTS upload_budget (
    day            TEXT PRIMARY KEY,   -- YYYY-MM-DD (UTC)
    bytes_uploaded INTEGER NOT NULL DEFAULT 0
);

-- Per-PAGE deliverables (image delivery): one row per graphical page
-- extracted from a classified deck/PDF. The delivery unit is the page
-- (a PNG render), not the source file. Exact dedup by image sha256,
-- near-dup by perceptual hash (phash). status flow:
--   pending    - selected, render not attempted yet
--   extracted  - PNG rendered and on disk (extract/ dir)
--   duplicate  - sha256 or phash matched an already-known image
--   delivered  - uploaded to Drive and verified
--   rejected   - render failed / not deliverable
CREATE TABLE IF NOT EXISTS pages (
    id                 INTEGER PRIMARY KEY,
    file_id            INTEGER NOT NULL REFERENCES files(id),
    page_index         INTEGER NOT NULL,
    sha256             TEXT,
    phash              TEXT,
    local_path         TEXT,              -- rendered PNG location; NULL after delivery
    status             TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','extracted','duplicate','delivered','rejected')),
    delivered_filename TEXT UNIQUE,
    batch_id           INTEGER,
    delivered_at       TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (file_id, page_index)
);
CREATE INDEX IF NOT EXISTS idx_pages_file     ON pages(file_id);
CREATE INDEX IF NOT EXISTS idx_pages_status   ON pages(status);
CREATE INDEX IF NOT EXISTS idx_pages_sha256   ON pages(sha256);
CREATE INDEX IF NOT EXISTS idx_pages_phash    ON pages(phash);

-- Harvest progress markers (e.g. completed Common Crawl parquet parts)
-- so multi-day discovery runs resume instead of rescanning.
CREATE TABLE IF NOT EXISTS harvest_cursor (
    source     TEXT PRIMARY KEY,
    cursor     TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Operational events (blocking incidents, parks, budget pauses) for status.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    domain     TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, created_at);
"""

MIGRATIONS: dict[int, str] = {
    # 2: image delivery -- audit_log gains per-page image columns. (The
    #     `pages` table itself is CREATE TABLE IF NOT EXISTS in DDL, so it
    #     appears on existing DBs automatically at next startup.)
    2: """
    ALTER TABLE audit_log ADD COLUMN page_index INTEGER;
    ALTER TABLE audit_log ADD COLUMN image_sha256 TEXT;
    ALTER TABLE audit_log ADD COLUMN phash TEXT;
    ALTER TABLE audit_log ADD COLUMN source_file_sha256 TEXT;
    ALTER TABLE audit_log ADD COLUMN extraction_method TEXT;
    """,
}


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the schema. Safe to call on every startup."""
    conn.executescript(DDL)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0
    if current == 0:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        for version in sorted(v for v in MIGRATIONS if v > current):
            conn.executescript(MIGRATIONS[version])
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()
