# PptxSweeper — Engineering Decisions Log

## Revised client criteria applied (criteria1.pdf / criteria2.pdf, 2026-07-03)

- **PPT/PPTX only** (client instruction overriding the PDFs' "PPTX or
  PDF"): enforced in `allowed_formats` at three layers — URL filter,
  download magic-byte check, classify validation — and harvesters no
  longer even queue .pdf links.
- **Source exclusions (Revised criteria §2) — PARTIALLY LIFTED by client
  instruction 2026-07-03**: Fortune 500 companies and US universities
  are INCLUDED again (SEC EDGAR harvester re-enabled; F500 IR seeds and
  university seeds restored). Prestigious US research centers / national
  labs / think tanks REMAIN excluded via `seeds/excluded_sources.txt`
  (filter reason `excluded_source:<domain>`; OSTI harvester stays
  unregistered) — the new instruction did not mention them; delete those
  entries if the client lifts that exclusion too.
- **No maximum slide count** (Revised §1) — already the case.
- **Metadata preservation (Revised §3/§4)**: every delivered file ships
  with `BATCH_NN_file_NNNNN.metadata.json` containing the manifest row,
  quality metrics + explanations, OOXML document properties
  (title/author/organization/created/modified/language, extracted from
  docProps at classify time), and the full raw discovery/crawl metadata.
  Missing source URL already blocks delivery (manifest backbone).

## Multi-machine operation & "Google Drive only" (client direction, 2026-07-03)

Client direction: no separate database service; Google Drive is the only
cloud storage; work runs on the local machine plus one or more GCP VMs,
all delivering into ONE shared Drive folder, with no duplicates.

How this is honored without corrupting state or violating the delivery
contract:

- **No database server anywhere.** The registry is a plain local file
  (`data/registry.db`, SQLite — an embedded file format, not a service;
  nothing to install, configure, or pay for). It is snapshotted and
  uploaded to Drive (`registry_backups/`) after every batch, so Drive
  holds the durable copy. Putting the live registry file *on* Drive is
  not done because Drive is not a safe filesystem for concurrent writes
  — two VMs writing the same file through Drive corrupts it silently.
- **No direct-to-Drive downloads.** The client contract requires every
  file to be validated, converted (.ppt→.pptx), quality-classified, and
  compliance-screened BEFORE delivery, and forbids rejects/LOW files in
  the delivery folder. That processing needs the file on a local disk.
  So: download to local temp → validate/classify → upload only accepted
  files. From the outside, accepted files flow into Drive continuously.
  (The original spec also explicitly forbids using a Drive mount as the
  download target for exactly this reason.)
- **Duplicate prevention across machines is by construction, not by
  locking:** each machine gets `NODE_ID` / `NODE_COUNT` (.env); domains
  are split by `sha1(domain) % NODE_COUNT`, so no two machines ever
  touch the same URL. Batch numbers are interleaved (node k of N
  creates batches k+1, k+1+N, ...) so folder/file names never collide.
  The residual case — the same file hosted on two different domains —
  is closed by `pptxsweeper sync-dedup`, which exchanges SHA256 lists
  through a `_dedup/` folder on Drive (each node writes only its own
  file, so Drive-level concurrency is safe).
- Google Drive authentication is per-machine via `rclone config`
  (documented in the README runbook, including headless-VM auth).

This file records reasonable decisions made to resolve ambiguity in the
spec, in build order. Nothing here contradicts the spec; each entry
either (a) picks a concrete value where the spec said "default"/"config"
without a number, or (b) chooses one valid implementation among several
where the spec allowed discretion.

## Stack

- **Python 3.11+**. Async I/O via `httpx` + `asyncio` for the downloader
  and harvesters (spec mandates httpx+asyncio explicitly). `click` for
  the CLI (`pptxsweeper <stage>` subcommands). `sqlite3` (stdlib) for the
  registry — no ORM; hand-written DDL + thin DAO layer, because the spec
  requires exact control over WAL mode, transaction boundaries, and
  `RETURNING`-based claim queries.
- **PyMuPDF (fitz)** for PDF parsing, **PIL + numpy** for raster image
  signal extraction, **lxml** for OOXML XML parsing (faster + more
  forgiving than stdlib `xml.etree` for real-world decks), **python-pptx**
  used only as a secondary cross-check, never as the primary parser
  (spec explicitly warns against relying solely on it).
- **pyarrow** for Common Crawl columnar index (parquet-over-HTTP range
  reads via `pyarrow.dataset` + `fsspec`/`s3fs` anonymous access).
- **pytesseract** (wrapping system `tesseract`) for the lightweight OCR
  fallback in image classification; it only runs when other signals are
  ambiguous, per spec. If `tesseract` is not installed, OCR signal is
  skipped and the classifier falls back to the other signals (never
  crashes — treated as "ambiguous, no text evidence").
- **rclone** invoked as a subprocess (not a Python rclone binding —
  none is officially maintained); all Drive I/O goes through `rclone
  copy` / `rclone check` / `rclone mkdir` / `rclone lsjson`.

## Registry / concurrency

- Single-writer pattern: every stage that mutates the DB owns exactly
  one `sqlite3.Connection` opened in WAL mode with
  `PRAGMA busy_timeout=30000`. The async downloader funnels all writes
  through one dedicated `asyncio.Task` reading off an `asyncio.Queue`
  (many producers / one consumer) so worker coroutines never touch the
  connection directly. `classify` and `package` are single-process,
  mostly-sequential stages, so they use the connection directly but
  still batch commits every `download.db_writer_batch_size` rows.
- Claim query uses `UPDATE ... RETURNING` (SQLite ≥ 3.35, verified
  available on target VPS images) instead of a SELECT-then-UPDATE
  round trip, to avoid a TOCTOU race between two stage instances if the
  operator accidentally runs a stage twice.
- On `SQLITE_BUSY` the DAO retries with exponential backoff (10ms →
  20ms → ... capped at 2s, 8 attempts) before surfacing the error.

## Batch / filename generator

- Padding: batch id `< 100` → 2-digit (`BATCH_00`..`BATCH_99`); once a
  batch id reaches 100 the folder/filename padding grows to 3 digits
  (`BATCH_100`) and stays at 3 digits for all subsequent batches (never
  reverts, never mixes widths within one run). Implemented as
  `max(2, len(str(batch_id)))`-style logic but pinned so the width only
  ever grows monotonically (stored on the `batches` row so a restart
  doesn't down-shift the width if an early batch is re-inspected).
- File counter resets to `00001` at the start of each batch and is
  strictly monotonic with no gaps within a finalized batch — enforced
  by storing `next_file_counter` on the `batches` row and incrementing
  it transactionally with each filename assignment (assignment and
  `files.delivered_filename` write happen in the same DB transaction).
- Batch numbers themselves are a global monotonic sequence from a
  `SQLite` `AUTOINCREMENT`-free counter (`MAX(batch_id)+1` under a
  transaction) — never reused even if a batch is later found to be
  short or is abandoned.

## Composition packager

- "Hold surplus MEDIUM in `reserve`" is implemented as: the packager
  scans `DELIVER`-decision files ordered oldest-first, provisionally
  assigns them to the open batch, and before finalizing checks the
  running HIGH/MEDIUM ratio. If MEDIUM would exceed 30% of the batch,
  excess MEDIUM files are set to `reserve` status (not consumed) and
  skipped for this batch; they remain eligible candidates for the next
  batch. This can stall a batch if HIGH supply is thin.
- To prevent an indefinite stall, `batch.composition.max_batch_open_days`
  (default 14) allows the packager to close an under-composition batch
  early with `composition_ok=0` recorded on the batch row (visible in
  `status`), rather than holding files hostage forever. This is a
  judgment call: the spec says "enforce composition per batch" but also
  implies unattended multi-week operation must make forward progress.
- 0% LOW is enforced structurally: LOW is a terminal `rejected` status
  and LOW files are never inserted into the DELIVER candidate pool in
  the first place (classify stage only ever produces DELIVER/REVIEW/
  REJECT decisions and LOW quality is always REJECT — see quality
  engine).

## Quality engine

- `classify(file_path) -> QualityReport` is a pure function: it takes a
  path (already-downloaded, already-validated file) and does not touch
  the network or the registry. The `classify` CLI stage is a thin
  wrapper that calls this function and persists the result.
- Structural filler detection uses layout heuristics: a slide/page is
  "filler" if (a) it is the first or last slide/page of the deck, or
  (b) its text matches title/agenda/section-divider/thank-you patterns
  (regex list, config-extendable) AND it has zero charts/diagrams/
  tables/analytical images. This mirrors common IR-deck conventions
  (title slide, agenda slide, "Thank You" / "Q&A" closer, section
  dividers between chapters).
- Borderline handling: any percentage-based threshold comparison uses
  `abs(value - threshold) <= quality.epsilon` to detect "within epsilon"
  and forces `REVIEW` instead of the value's natural side of the
  threshold, per spec ("never silently DELIVER").
- Chart-as-image false-rejection fix (the named prior bug): OOXML charts
  (`graphicData` URI ending in `/chart`) are always counted as
  `native_chart_count` regardless of any raster fallback image also
  present in the same shape; raster image classification is only run on
  images that are NOT the fallback/preview bitmap of a native chart
  object (identified via the `<c:chart>` relationship and blip fallback
  relationship IDs), so a chart is never double-penalized as a "photo".

## Tier 1 reality check (verified against live endpoints, 2026-07-03)

Three of the spec's Tier 1 assumptions turned out to be wrong in
practice; the harvesters were reworked accordingly:

1. **Anonymous S3 access to `s3://commoncrawl` is denied**
   (`ACCESS_DENIED on HeadObject`). The supported free path is HTTPS via
   `data.commoncrawl.org`. Each crawl publishes
   `crawl-data/{CRAWL}/cc-index-table.paths.gz` listing its ~300
   columnar-index parquet parts; the harvester range-reads those with a
   custom httpx-backed random-access file (no AWS deps at all), reading
   only the `url` + `fetch_status` columns. Verified: footer+schema read
   works; 1.4M rows scan in ~10s.
2. **CDX APIs cannot do suffix searches** ("all URLs ending .pptx").
   CDX indexes are SURT/domain-keyed; only exact `filter=mime:...`
   filters work (URL regex filters return "No Captures found" — tested).
   So the CDX paths (CC fallback + Wayback harvester) are DOMAIN-SCOPED:
   they walk `seeds/cdx_domains.txt` plus every domain already in the
   registry, filtered by the two PowerPoint MIME types.
3. **Common Crawl contains essentially zero PowerPoint captures.**
   Tested: 4.3M-row raw parquet scan -> 0 `.pptx` URLs (vs 35k `.pdf` in
   2.8M rows); mime-filter probes on unece.org / fao.org / nist.gov /
   epa.gov / who.int across crawls 2018→2025 -> zero captures, while
   Wayback returns PowerPoint captures for the same domains immediately.
   Consequence: **Wayback CDX is the real Tier 1 backbone for
   .ppt/.pptx**, and Common Crawl is useful mainly for PDF slide decks —
   `.pdf` is added to `common_crawl.extensions` (the quality engine
   rejects non-deck PDFs; per-domain caps bound the flood). Multi-node:
   parquet parts are sharded by `part_index % NODE_COUNT`, and completed
   parts are checkpointed in `harvest_cursor` so multi-day scans resume.

## Harvesters

- Tier 4/5/6 are implemented primarily through one **generic,
  YAML-configurable harvester** (`harvesters/generic.py`) supporting
  `sitemap`, `oai_pmh`, `api_json`, and `listing_page` discovery methods
  per source, as the spec explicitly permits ("Generic YAML-configurable
  source spec... reuse the existing PptxSweeper registry format"). Seed
  files (`seeds/central_banks.csv`, `seeds/ocw_sites.csv`,
  `seeds/sources/*.yaml`) are human-editable and best-effort, per spec.
  Dedicated Python adapters are hand-written only where an API has a
  materially different shape (Q4 Inc., SEC EDGAR, OSTI, World Bank OKR,
  Zenodo/Figshare/OSF/Internet Archive/GitHub Code Search) — these are
  the ones explicitly named as needing bespoke handling or being ported
  from prior work.
- Tier 6 harvesters are freshly written against each service's public
  API (Zenodo, Figshare, OSF, Internet Archive, GitHub code search)
  rather than "ported" from a prior codebase, since no such prior
  PptxSweeper code exists in this repository — there is nothing to port
  from. Interface and behavior match the spec's description of what
  those harvesters should do.
- Common Crawl: primary path is `pyarrow.dataset` range-reads over the
  public columnar index via anonymous S3/HTTPS; if that import/path
  fails at runtime (network policy, missing `s3fs`, etc.) the harvester
  transparently falls back to the per-crawl CDX API, exactly as the
  spec allows ("fall back to the CDX API per crawl if parquet access
  fails").

## Politeness

- Domain sharding: `hash(domain) % worker_count` assigns a domain to
  exactly one worker for the lifetime of a run; a worker only ever
  claims URLs from `urls.domain` values in its shard, so a domain is
  never touched concurrently even though claims happen through a shared
  DB (belt-and-suspenders alongside the DB-level claim).
- Circuit breaker backoff stages (30s/5min/30min) are per-domain and
  reset on any 2xx response; three *consecutive* non-2xx (403/429/5xx)
  triggers park; a domain parked a second time (i.e. it fails again
  after being un-parked and retried) is permanently blacklisted, per
  spec. Both counters (`failure_streak`, a separate `park_count`) are
  persisted on the `domains` row so this survives restarts.

## Delivery / rclone

- Verification defaults to `rclone check --size-only` (config-toggle to
  full hash check) because Drive's own checksum support varies by file
  type and size-only is what the spec lists as the default-acceptable
  path ("or hash check where the remote supports it").
- Registry backup uses `VACUUM INTO` (available SQLite ≥ 3.27) to
  produce a consistent snapshot without locking out the next stage,
  then `gzip`s it before upload.

## Misc

- `.ppt` → `.pptx` conversion runs `soffice --headless --convert-to
  pptx` inside a per-call temp `-env:UserInstallation=` profile
  directory (avoids shared-profile lock contention when
  `conversion.max_concurrent` > 1) with a hard `subprocess` timeout;
  timeout or non-zero exit or missing output file = REJECT with reason
  `ppt_conversion_failed`.
- PII/minors/prohibited screens are regex+keyword heuristics as
  explicitly specified ("lightweight regex pass", "keyword+context
  heuristic") — not ML classifiers. This is a deliberate scope match to
  the spec, not a shortcut; upgrading to an ML-based screen is a
  documented future improvement, not part of this build.
