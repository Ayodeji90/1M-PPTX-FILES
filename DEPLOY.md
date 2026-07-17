# Deployment runbook — fresh Drive + multiple machines

Goal: run PptxSweeper on your laptop + N GCP VMs, all delivering into ONE
new Google Drive folder, splitting the work with no coordination.

## 0. One-time prep (do once, anywhere)

- **New Google Drive:** nothing to pre-create. The pipeline makes
  `PptxSweeper_Delivery/` on first run. Just decide which Google account
  owns it — the SAME account must be connected on every machine.
- **Brave Search API key:** sign up at https://brave.com/search/api/
  (free tier ~2000 queries/month; paid tiers scale). You'll paste it into
  each machine's `.env` as `BRAVE_API_KEY`.
- **(Optional) prior-collection dedup:** if you already have ~30k files on
  an old Drive folder, connect that old remote too (e.g. `gdrive_old`) and
  on ONE machine run:
  ```bash
  rclone hashsum sha256 gdrive_old:OldDeliveryFolder > prior_hashes.txt
  ./.venv/bin/pptxsweeper import-catalog prior_hashes.txt
  ```
  Copy the resulting `data/registry.db`'s known-hash seed to each VM, OR
  just run the two lines on each machine — either way those hashes are
  skipped. (Duplicates are cheap; skip this if it's a hassle.)

## 1. Each machine (laptop + every VM)

```bash
git clone https://github.com/Ayodeji90/1M-PPTX-FILES.git ~/pptxsweeper
cd ~/pptxsweeper
bash bootstrap.sh          # installs everything; prints next steps
nano .env                  # see step 2
rclone config              # connect Google Drive as remote "gdrive"
tmux new -s sweeper
./.venv/bin/pptxsweeper run
# detach: Ctrl-B then D
```

## 2. `.env` per machine — the ONLY thing that differs

Work is split by `NODE_ID` / `NODE_COUNT`. Set `NODE_COUNT` to the TOTAL
number of machines on EVERY machine; give each a unique `NODE_ID`.

| machine     | NODE_ID | NODE_COUNT |
|-------------|---------|------------|
| laptop      | 0       | 3          |
| VM 1        | 1       | 3          |
| VM 2        | 2       | 3          |

Also set `CONTACT_EMAIL` (goes in the polite crawler User-Agent) and
`BRAVE_API_KEY`. **If you add a machine later, bump `NODE_COUNT` on ALL of
them.**

Discovery is now sharded at harvest time, so no two machines crawl the
same domain/page — no duplicate work, no duplicate uploads.

## 3. GCP VM sizing

`e2-standard-2` (2 vCPU / 8 GB), Ubuntu 22.04/24.04, 50–100 GB disk. The
downloader is I/O-bound at `concurrency: 300`; payloads are uploaded to
Drive and deleted per batch, so disk stays small.

## 4. Watch it

```bash
./.venv/bin/pptxsweeper status         # counts, ETA, per-tier acceptance
tmux attach -t sweeper                  # live progress line
```

Google's upload cap is 750 GB/account/day — shared across all machines
since they use one account. The packager self-throttles under
`upload.daily_byte_budget_gb` (700).
