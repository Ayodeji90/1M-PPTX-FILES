# Image Delivery Implementation Plan

**Status:** Phase 1 BUILT + TESTED locally (126 tests pass). Not yet deployed
or enabled on the VMs — `delivery.image: false` everywhere until you say go.
**Goal:** Deliver up to **1,000,000 individual graphical-page images** (PNG) extracted
from PPT/PDF files (and, if the client requires source URLs, from public web pages),
with image-level duplicate control and full source traceability. Existing
downloaded-but-undelivered files are post-processed into the new format; new
downloads flow through the new pipeline from day one.

---

## 1. What the client now requires (summary of the changed contract)

| Requirement | Detail | Impact on pipeline |
|---|---|---|
| Deliverables are **images**, not decks | Each *graphical page* saved as an individual PNG. "Graphical" = analytical graphics with data / valuable visual pages (charts, diagrams, tables, infographic-style pages). | New EXTRACT stage renders selected pages to PNG; package stage delivers PNGs. |
| **No content editing** | Pages must be delivered in their original form — no removing elements, no cropping, no overlays. | Rendering is lossless, page-accurate. Never post-edit pixels. |
| **No low-value photos** | Pure photos / exhibit images are non-compliant deliverables. | Per-page selection excludes photo-heavy pages (existing `is_photo_heavy` signal). |
| **Duplicate control ≈ ≤3%** | Measured by percentage at acceptance. Image dedup is technically different from PPT dedup (near-identical ≠ byte-identical). | Two-layer dedup: exact SHA256 + perceptual hash (pHash/dHash) near-dup detection, synced across all VMs. |
| **Never reuse pages from previously delivered PPT decks** | Content from past deliveries is off-limits for this delivery. | Delivered-content hash corpus consulted before any extraction is delivered. |
| **Dedicated external dedup tool** | A separate infographic dedup system is being built and will be used by all vendors as the final gate. | Pipeline exposes a pluggable external-tool gate per batch (interface TBD). |
| **No AI-generated images** | Large batches of AI images (simulated or real data) are rejected by the client. | Content is extracted from real files, never generated — low inherent risk; we still keep the low-value-photo exclusion. No in-pipeline AI detection for now (decision: not needed since we don't prompt-generate). |
| **Source URLs** | Client deciding: (a) URLs required for all images → must source from the open web; (b) not required → batch-fetch files and extract. | Default = (b) batch-fetch + extract. If (a): add source URL + page index to every manifest row (already captured) and enable optional web-page capture. |
| **1M target** | 1,000,000 **images** delivered (not decks). | Requires ~200k–350k decks (≈3–5 graphical pages each) → more sources, incl. PDF. |

---

## 2. Current pipeline vs. what's needed (gap analysis)

```
CURRENT:                                                        NEW:
harvest ppt/pptx URLs                                           harvest ppt/pptx/pdf URLs (+web pages if URLs required)
  │                                                               │
download (validate, sha256 dedup, wayback)                      download (same, + pdf allowed)
  │                                                               │
classify (per-slide features, quality, compliance)              classify (same; pdf already supported by quality engine)
  │                                                               │
  │                                                               ▼
  │                                                        ★ NEW: EXTRACT  — select graphical pages → render PNG
  │                                                               │
  │                                                        ★ NEW: IMAGE DEDUP — sha256 + perceptual hash, cross-VM
  │                                                               │
package .pptx batches → Drive                                   package PNG batches (per-image manifests) → Drive
                                                                  │
                                                              ★ NEW: external dedup tool gate (final, per batch)
```

**What already exists and is reused (verified in code):**
- Per-slide feature vectors (`SlideFeatures`: native chart / diagram / table /
  analytical-image counts, photo-heavy, text-only, structural-filler) —
  already persisted as JSON on every classified file. Page selection is mostly free.
- PDF quality engine (`quality/pdf.py`, `extract_page_features`) and `files.format`
  already admit `pdf`; the downloader already sniffs PDF magic bytes.
- SHA256 dedup + cross-VM hash exchange via Drive `_dedup/` folder
  (`dedup_sync.py`, `known_hashes` table).
- LibreOffice headless conversion infra (per-call profile, timeout, process-group
  kill, TMPDIR isolation) — the exact pattern the renderer needs.
- Batch packaging with per-file metadata sidecars + manifests + composition
  rules + upload budget + verification.
- Source-URL traceability (manifest rows carry `source_url`, `download_url`,
  `collection_ts`, `http_status`, `retrieval_method`).

**What must change:**
1. `allowed_formats` gains `pdf` (config + URL filters + Common Crawl extensions).
2. New EXTRACT stage (renders selected pages → PNG).
3. New image-level dedup (perceptual hashing + corpus sync).
4. Package stage delivers PNGs with extended manifest columns.
5. Post-processing pass over already-downloaded, undelivered files.
6. Optional web-page capture mode (only if client requires URLs).
7. External dedup-tool gate (pluggable; interface TBD).

---

## 3. Architecture changes by stage

### 3.1 Sources & download — add PDF, keep everything else
- `config.yaml`: `allowed_formats: ["pptx", "ppt", "pdf"]`.
- URL filter: allow `.pdf` (the filter/extension gates currently drop it);
  Common Crawl harvester `extensions` list gains `.pdf`.
- Downloader: magic sniff already maps `pdf`; the `allowed` set comes from
  config, so no code change — just config.
- **PDF volume is the unlock for 1M images**: reports, conference papers, and
  slide-deck PDFs are far more abundant than ppt/pptx. All existing harvesters
  (CDX, sitemaps, CKAN, Zenodo/Figshare/OSF, Brave dorking) already surface PDFs —
  they were being filtered out.
- **Web-page capture (only if client requires URLs):** new source type —
  headless Chrome renders a full-page PNG of a public page. Design as a
  pluggable "capture" backend alongside the file-based extractor. Deferred
  until the client's URL decision lands.

### 3.2 Classify — mostly unchanged; add a per-page "graphical" flag
- Deck-level HIGH/MEDIUM/LOW and compliance screening stay exactly as-is.
- Compute per-page deliverability from the already-persisted `feature_vectors`:
  a page is **extractable** when it is graphical (chart/diagram/analytical
  object or analytical vector work) AND not photo-heavy AND not structural
  filler AND not text-only.
- Store the selection as `pages_json` on the file row (list of
  `{index, graphical, reason}`) — a pure function of existing vectors, so
  re-running after threshold changes is free.

### 3.3 NEW — EXTRACT stage (render graphical pages → PNG)
**Input:** classified files (payload + `feature_vectors`/`pages_json`).
**Output:** one PNG per selected page, plus a per-page metadata sidecar.

- **Rendering pipeline:**
  - `.pptx` → `soffice --headless --convert-to pdf` (reuse conversion infra:
    per-call profile, timeout, SIGKILL process group, TMPDIR isolation) →
    `pdftoppm -png -f <n> -l <n> -r 150` (poppler-utils) for each selected page.
  - `.ppt` → convert to `.pptx` first (existing path), then as above.
  - `.pdf` → `pdftoppm` directly on selected pages.
  - *(future)* web pages → headless Chrome `--screenshot` full-page PNG.
- **Fidelity:** page-accurate, original form only — no cropping, no element
  removal, no watermarking. `-r 150` DPI keeps charts crisp while bounding
  file size (~0.5–2 MB/page). DPI is config.
- **Idempotent & resumable:** page renders keyed by `(file_sha256, page_index)`
  in a `pages` table; a crashed run re-renders only missing pages.
- **Resource control:** rendering is CPU-heavy (soffice + pdftoppm). Own
  concurrency cap (`extract.max_concurrent`, start 1–2 on the 4-vCPU VMs),
  own staging dir, and the existing disk/RAM guards apply. Pairs with
  `classify.workers` so the two heavy stages don't starve each other.
- **Disk impact:** PNGs add ~0.5–2 MB/page to staging until delivered and
  deleted. With ~3–5 pages/deck that's ~2–10 MB/deck — the existing
  `max_downloaded_backlog` + disk-floor guards keep this bounded; expand the
  VMs' 29 GB disks when we start extract-heavy runs.

### 3.4 NEW — image-level dedup (two layers + external gate)
**Layer 1 — exact:** SHA256 of the rendered PNG. Reuses `known_hashes`
infrastructure; a PNG whose bytes already exist anywhere is dropped.

**Layer 2 — perceptual near-dup:** a 64-bit dHash (or pHash) per PNG;
Hamming distance ≤ threshold (config, start ~10/64) = near-duplicate.
New `image_hashes` table: `(phash, sha256, file_sha256, page_index,
source_url, delivered_at, status)`.

- **Cross-VM sync:** extend the existing `_dedup/` Drive exchange to carry
  image hashes (`node_{id}_image_hashes.txt.gz`, append-only like the current
  hash lists). Every VM consults the union before delivering; a page whose
  pHash matches anything already delivered (by us, any VM) is dropped or
  quarantined to review.
- **Delivered-content corpus (the "never reuse previous delivery" commitment):**
  - Baseline (cheap): register SHA256 of every previously delivered file
    (already in `audit_log`) into `known_hashes` — prevents re-downloading the
    same deck bytes.
  - Page-level (thorough): build a corpus of page hashes from decks already
    delivered. Those payloads were deleted post-upload, so this requires
    re-fetching the ~12k delivered files once from the old Drive account (or
    their source URLs) and running extract → hash → store. This is a one-time
    batch job; flag as an optional phase depending on how strict the client's
    acceptance is. **Note:** pages from previously delivered decks are
    off-limits *as content* — this corpus exists only to block them, never to
    deliver them again.
- **External tool gate (their infographic dedup system):** pluggable hook —
  before a batch finalizes, the batch dir is handed to the external tool
  (CLI or API, interface TBD once the tool ships); its output (list of
  duplicates) is removed from the batch. Until then the hook is a no-op passthrough.

### 3.5 Package stage — deliver PNGs
- Naming: `BATCH_NN_img_NNNNN.png` (counter continues per batch as today;
  ext becomes `.png`).
- **Manifest columns extended** (per delivered image):
  `page_index`, `image_sha256`, `phash`, `source_file_sha256`,
  `extraction_method` (libreoffice/pdftoppm/chrome), `source_url`,
  `original_filename`, `quality_class`, page-feature summary. Everything else
  (compliance screens, retrieval method, timestamps) stays.
- **Composition:** client's 70% HIGH / 20–30% MEDIUM / 0% LOW rule now applies
  per *image*: HIGH = chart/diagram/analytical page; MEDIUM = graphical but
  simpler (table-only, single-chart pages); LOW/photo/text-only = never.
  The existing `select_for_batch` + streaming composition logic is reused
  with the image-level class.
- Metadata sidecar per PNG (existing `*.metadata.json` pattern) carries the
  full provenance record.

### 3.6 Post-processing the already-downloaded files
**Scope — files downloaded but NOT delivered** (~17k downloaded − ~12k
delivered ≈ 5k files across VM1/VM2 disks, plus queue rows). Delivered decks
are skipped: their content is off-limits anyway (never-reuse rule), so no
re-delivery of their pages.

- One-time batch job per VM (`extract --postprocess`): for every file with
  `decision IN (DELIVER, REVIEW)` and a payload still on disk (or in the
  queue), run select → render → dedup → package as PNGs.
- Files whose payloads were deleted (delivered or cleaned) are skipped unless
  we explicitly re-fetch — which we won't for delivered decks (see rule above).
- Runs under the same disk/RAM/CPU guards; resumable.

### 3.7 Deployment / ops
- Config profile per VM (`config.vm3.yaml` pattern) + `extract` section.
- New systemd unit unchanged — the orchestrator gains the extract stage in the
  deliver loop (classify → extract → dedup → package), same restart/health
  semantics.
- Deployment order: push code → pull on all 3 VMs → restart → verify.
- **Scale math for 1M images:** at ~3–5 graphical pages/deck we need
  ~200k–350k decks. Current queues: ~124k URLs across VMs. PDFs unlock far
  more volume; expect to add PDF-oriented seeds/sources (existing harvesters
  already surface them once the filter allows).

---

## 3.9 Phase 1 status — what is built and tested

Done and covered by unit tests (126 passing, incl. ~20 new):
- **PDF unlock**: `allowed_formats` + Common Crawl extensions + harvesters'
  `DOC_EXTENSIONS` now admit `.pdf`; the downloader/validator/quality engine
  already supported PDFs, so this is purely config/seed changes.
- **`pages` table** (schema v2 + migration): per-page rows with sha256, phash,
  local_path, status (pending/extracted/duplicate/delivered/rejected).
- **Page selection** (`extract/select.py`): pure function of persisted per-slide
  feature vectors — chart/diagram/table/OLE/vector pages qualify; photo-heavy,
  text-only and structural-filler pages never do.
- **Renderer** (`extract/render.py`): soffice -> PDF -> pdftoppm per page;
  verified end-to-end with REAL soffice + pdftoppm (fixture deck -> 5 PNGs,
  page-accurate, original form).
- **Extract stage** (`stages/extract_stage.py`): bounded thread pool, disk
  floor guard, sha256 + dHash dedup, idempotent/resumable. IMPORTANT fix found
  during real testing: near-dup is only compared across DIFFERENT source files
  — several similar-styled chart pages of ONE deck are all distinct
  deliverables (the client's own example: 30-40 pages of a 70-80 page deck).
- **Image-mode packaging**: `BATCH_NN_img_NNNNN.png`, page candidates,
  page-level delivered marking, audit rows + manifests with page_index,
  image_sha256, phash, source_file_sha256, extraction_method.
- **CLI `extract`** + orchestrator runs extract before package when
  `delivery.image: true`.
- **Latent production bug fixed**: `manifest_filename()` was called with 3
  args (node_id) after the signature dropped it in July — every batch finalize
  would have thrown TypeError. Found by the new end-to-end package test.

Still to do (later phases): cross-VM image-hash sync via `_dedup/` (P2),
external dedup-tool CLI gate (P4), post-processing pass over undelivered
files (P3), web-page capture if client requires URLs (P5), delivered-content
page corpus (P6).

## 4. Phased rollout

| Phase | Scope | Exit criterion |
|---|---|---|
| **P0** | Requirements freeze: client URL decision, external dedup-tool interface, per-image composition confirmation | All open questions answered |
| **P1** | Extract stage + `pdf` allowed + PNG packaging + extended manifests (VM1 only, small batches) | A batch of PNGs delivered with full provenance; 98 tests pass |
| **P2** | Image dedup in-pipeline: pHash + `image_hashes` table + cross-VM `_dedup` sync | Two VMs agree on a dup set; no near-dup delivered in a test batch |
| **P3** | Post-processing pass over undelivered downloaded files on all VMs | All undelivered decks extracted; PNGs flow through normal delivery |
| **P4** | External dedup-tool gate integration (interface from P0) | Batch gate runs before every finalize |
| **P5** | Source expansion for 1M scale (PDF harvesters, more seeds, web capture if URLs required) | Sustained download+extract throughput, no disk/RAM stalls |
| **P6** | Optional: page-hash corpus from previously delivered decks (re-fetch once, hash only, never re-deliver) | Corpus active in `_dedup` sync |

---

## 5. Open decisions needed from you

1. **Client URL decision** — if URLs are required (option a), we enable
   web-page capture (headless Chrome) and add a per-image source-URL column to
   the manifest (we already record it, so cost is small). If not required,
   skip capture entirely.
2. **External dedup tool interface** — when it's ready: CLI (batch dir in →
   dup list out), HTTP API, or Drive-folder exchange? We'll build the shim to
   match.
3. **Composition confirmation** — does the 70/30 HIGH/MEDIUM per-batch rule
   apply to images the same way it did to decks? (Assumed yes.)
4. **Previously-delivered corpus** — do the ~12k delivered files need their
   *pages* hashed into the block corpus (P6, one-time re-fetch), or is
   file-level SHA256 blocking + the external tool enough?
5. **Image spec** — is 150 DPI PNG acceptable as the default render
   resolution (lossless, ~0.5–2 MB/page)? Any minimum/maximum dimension or
   size contract from the client?
