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

## 2b. Second VM as a download-only consumer (URL handoff)

Instead of the symmetric node-split, you can run VM2 as a pure
**consumer**: it does no discovery, takes a share of VM1's already-found
URLs, and delivers to its **own folder** on the same Drive account.

On **VM1 (producer)** — keep it harvesting, and have it hand off a share:
```bash
# .env: normal setup (NODE_ID=0, NODE_COUNT=1 is fine here)
pm2 start ./.venv/bin/pptxsweeper --name sweeper --interpreter none \
  --kill-timeout 90000 -- run --handoff producer
```

On **VM2 (consumer)** — its own delivery folder, no harvesting:
```bash
# .env: set a distinct delivery folder and a different node id
#   RCLONE_ROOT_FOLDER=PptxSweeper_Delivery_VM2
#   NODE_ID=1        NODE_COUNT=2
pm2 start ./.venv/bin/pptxsweeper --name sweeper --interpreter none \
  --kill-timeout 90000 -- run --no-harvest --handoff consumer
```

How it works: every `multi_node.handoff_interval_hours` (default 10),
VM1 exports `handoff_fraction` (default 0.6 = 60%) of its *discovered*
backlog to a shared Drive folder `PptxSweeper_Handoff/` and marks those
URLs handed-off so **VM1 never downloads them**. VM2 imports them and
downloads/validates/delivers to `PptxSweeper_Delivery_VM2/`. Selection is
a stable URL hash, so the same 60% always goes to VM2 and nothing is
handed twice. Tune the split/interval in `config.yaml` under `multi_node`.

You can also run it by hand:
```bash
# on VM1:
./.venv/bin/pptxsweeper export-urls --fraction 0.6
# on VM2:
./.venv/bin/pptxsweeper import-urls
```

## 3. VM sizing (GCP or Azure free-tier / credit)

Minimum workable: **1 vCPU / 1 GB** (Azure free-tier `B1s`, 750 h/month,
$0) — the pipeline is RAM-aware: classify caps its worker processes to
what the box can hold, conversions are serialized, and downloads pause on
disk pressure.

Comfortable (recommended if you have free credit): **2 vCPU / 8 GB**
(e.g. Azure `Standard_D2ads_v7` — ~$83/month, so a ~$200 first-timer
credit covers ~2.4 months; `B2s` 2 vCPU/4 GB at ~$39/month stretches it
twice as far). Ubuntu 22.04/24.04, **64 GB disk** (downloads buffer on
local disk before upload; the free-space guard pauses rather than crashes,
but headroom avoids stalls). Everything is resumable, so you can downsize
or move VMs anytime without losing progress.

On the VM, run the tuned profile instead of the laptop defaults:

```bash
# in .env on the VM only:
PPTXSWEEPER_OVERRIDE=config.vm.yaml
```

That deep-merges `config.vm.yaml` (32 download workers, 2 classify
workers, 2 LibreOffice conversions, tighter disk/backlog caps) over
`config.yaml`. The laptop keeps the defaults — same repo, no drift.

## 4. Run it 24/7 with systemd (no SSH session needed)

Better than tmux for a headless VM: the service restarts itself on
crashes/reboots and starts at boot.

```bash
sudo cp deploy/pptxsweeper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pptxsweeper

systemctl status pptxsweeper
journalctl -u pptxsweeper -f          # live logs
```

Optional safety valve for small VMs (1 GiB B1s especially):

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile \
  && sudo mkswap /swapfile && sudo swapon /swapfile
# add to /etc/fstab:  /swapfile none swap sw 0 0
```

## 5. Watch it

```bash
./.venv/bin/pptxsweeper status         # counts, ETA, per-tier acceptance
tmux attach -t sweeper                  # live progress line (if using tmux)
```

Google's upload cap is 750 GB/account/day — shared across all machines
since they use one account. The packager self-throttles under
`upload.daily_byte_budget_gb` (700).

### Cost note (Azure free credit)

- The **750 h/month `B1s` free tier** is $0 for 12 months but slow (1 vCPU,
  1 GiB — expect single-digit files/minute on big decks).
- The **first-timer credit** is better spent on a `B2s`/`D2ads_v7` and
  runs the full pipeline at laptop-class speed. Track spending in the
  Azure portal and set a budget alert so the credit isn't silently
  exceeded — the pipeline is resumable, so you can downsize or pause it
  when the credit runs out.
