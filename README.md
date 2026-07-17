# PptxSweeper

Million-scale **.ppt/.pptx** acquisition pipeline. One command per
machine discovers URLs, downloads politely, validates + quality-checks
against the client criteria, and continuously uploads accepted files to
a **single shared Google Drive folder** — each file with its own
`metadata.json` sidecar, plus a per-batch manifest CSV and audit trail.

PDF is banned end-to-end (`allowed_formats` in config.yaml — enforced at
URL filter, download magic-bytes, and classify).

---

## 0. Starting fresh without re-downloading your existing files

If you already collected files into another Drive folder, seed their
SHA256 hashes so the pipeline never re-downloads that content — from ANY
source (the same deck is often hosted on many sites, so avoiding sources
is NOT enough; content-hash dedup is). Drive stores a SHA256 per file, so
you don't have to download anything:

```bash
# 1. Pull the existing files' hashes straight off the old Drive folder:
rclone hashsum sha256 gdrive_old:OldDeliveryFolder > prior_hashes.txt
# 2. Seed them into the registry (they're now permanently skipped):
.venv/bin/pptxsweeper import-catalog prior_hashes.txt
```

The downloader drops any payload whose SHA256 is already known, so overlap
with the new sources is handled automatically. (Native `.pptx` dedups
exactly; the minority that were `.ppt`→`.pptx` converted may download once
before de-duping among themselves.) To also get search-engine coverage,
set `BRAVE_API_KEY` in `.env` (see `.env.example`).

## 1. Run locally (this machine — already set up)

```bash
cd ~/WORKSPACE/AI_ML_PROJS/1_MILLION_PPTX
.venv/bin/pptxsweeper run
```

That's it. Ctrl+C stops it; re-running resumes. Progress line prints
every minute; details in `data/logs/*.jsonl`; dashboard any time with
`.venv/bin/pptxsweeper status`.

First-time setup on a NEW machine (laptop or VM) is section 3.

## 2. What lands on Google Drive

```
PptxSweeper_Delivery/
├── BATCH_01/
│   ├── BATCH_01_file_00001.pptx
│   ├── BATCH_01_file_00001.metadata.json   ← per-file metadata sidecar
│   ├── BATCH_01_file_00002.pptx
│   ├── BATCH_01_file_00002.metadata.json
│   ├── ...
│   └── BATCH_01_manifest.csv               ← contractual audit manifest
├── BATCH_02/ ...
├── _review/            ← borderline files held for manual triage
├── _status/            ← status.json from every machine
├── _dedup/             ← hash exchange between machines (automatic)
└── registry_backups/   ← database snapshots after every batch
```

The `metadata.json` sidecar carries: source URL + domain, download URL,
original filename, timestamps, HTTP/robots status, retrieval method,
public-access status, all compliance screen results, quality class +
metrics + explanations, document properties (title/author/organization/
dates/language), and the raw crawl metadata. Every delivered file also
has a row in the manifest CSV and in the registry audit log.

A batch uploads once 5,000 accepted files are ready (`batch.size` in
config.yaml — set it to e.g. 20 temporarily to watch a test batch land).

## 3. Set up a new machine (GCP VM or another computer)

**A. Create the VM (GCP console)** — e2-standard-2 (2 vCPU/8GB),
Ubuntu 22.04 or 24.04, 50–100 GB disk, region anywhere. SSH in.

**B. Install + configure (copy-paste block):**

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git rclone libreoffice tmux
# get the code onto the VM (from your laptop):
#   rsync -a --exclude .venv --exclude data ~/WORKSPACE/AI_ML_PROJS/1_MILLION_PPTX/ VM_IP:~/pptxsweeper/
cd ~/pptxsweeper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
cp .env.example .env && nano .env     # see step D
```

**C. Connect Google Drive (same Google account on every machine!):**

```bash
rclone config
# n (new remote) -> name: gdrive -> storage: drive -> defaults ->
# "Use web browser to automatically authenticate?" -> n (VM has no browser)
# On your LAPTOP run:  rclone authorize "drive"
# log in, copy the token it prints, paste it into the VM prompt.
```

Because every machine uses the same account + remote name `gdrive`,
they all deliver into the SAME `PptxSweeper_Delivery` folder.

**D. Give each machine its number (`.env`):** work is split by these
two values — no machine ever touches another's websites, and batch
numbers never collide, so no duplicates in Drive.

| machine        | NODE_ID | NODE_COUNT |
|----------------|---------|------------|
| your laptop    | 0       | 3          |
| VM 1           | 1       | 3          |
| VM 2           | 2       | 3          |

Also set `CONTACT_EMAIL=your@email` (goes into the polite crawler
User-Agent). **Update NODE_COUNT on ALL machines when you add one.**

**E. Run it (inside tmux so it survives you logging out):**

```bash
tmux new -s sweeper
.venv/bin/pptxsweeper run
# detach: Ctrl+B then D      reattach later: tmux attach -t sweeper
```

## 4. Useful commands

```bash
pptxsweeper status                 # dashboard: counts, ETA, per-tier acceptance
pptxsweeper status --sync          # also upload status.json to Drive _status/
pptxsweeper run --harvest-limit 100   # quick test drive
pptxsweeper import-catalog PATH    # seed prior SHA256 catalog (never re-download)
pptxsweeper classify --reclassify  # re-run quality decisions after threshold changes
pptxsweeper sync-dedup             # manual cross-machine hash exchange (auto in `run`)
sqlite3 data/registry.db 'SELECT status,COUNT(*) FROM urls GROUP BY 1;'
```

## 5. Client-criteria compliance map

- **ppt/pptx only, no PDF** — `allowed_formats`, 3 enforcement layers
- **min 5 slides, no max** — quality engine (`quality.min_slides`)
- **open-without-corruption** — magic bytes + full open validation; .ppt
  converted to .pptx via headless LibreOffice (failures rejected)
- **unique names / batch ID** — `BATCH_NN_file_NNNNN.pptx` scheme
- **source URL + audit record** — manifest CSV + sidecar + audit_log
- **excluded sources (F500, elite US unis, US research labs)** —
  `seeds/excluded_sources.txt` (+ OSTI/EDGAR harvesters disabled)
- **pirate blocklist / robots.txt / PII / COPPA / prohibited content** —
  compliance screens, results recorded per file
- **HIGH/MEDIUM only, ≥70% HIGH, 20–30% MEDIUM, 0% LOW** — quality
  engine + composition-aware batch packager (LOW auto-rejected;
  borderline → `_review/` on Drive)

## 6. Recovery

- **Crash / reboot / Ctrl+C:** just run `pptxsweeper run` again — every
  stage resumes; interrupted batches are reconciled against Drive
  (missing files re-uploaded, numbering never breaks).
- **Disk full:** downloads auto-pause under 20 GB free; delete
  `data/tmp_downloads/*` leftovers if needed and rerun.
- **Restore registry:** download the latest
  `registry_backups/*.db.gz` from Drive, `gunzip` it to
  `data/registry.db`.
- **Thresholds changed?** `pptxsweeper classify --reclassify` re-scores
  everything from stored feature vectors without re-downloading.
