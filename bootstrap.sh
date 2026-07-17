#!/usr/bin/env bash
# PptxSweeper one-shot machine setup (Ubuntu 22.04/24.04).
#
#   git clone https://github.com/Ayodeji90/1M-PPTX-FILES.git ~/pptxsweeper
#   cd ~/pptxsweeper && bash bootstrap.sh
#
# Idempotent: safe to re-run. After it finishes, do the 3 manual steps it
# prints (edit .env, rclone config, then `pptxsweeper run`). See DEPLOY.md.
set -euo pipefail

cd "$(dirname "$0")"
echo "== PptxSweeper bootstrap in $(pwd) =="

# 1. System packages (async downloader, LibreOffice for .ppt->.pptx,
#    rclone for Drive, tmux to survive SSH logout, tesseract for OCR).
if command -v apt >/dev/null 2>&1; then
  echo "-- installing system packages (sudo) --"
  sudo apt-get update -y
  sudo apt-get install -y python3-venv python3-pip git rclone libreoffice \
                          tmux tesseract-ocr
fi

# 2. Python virtualenv + deps.
if [ ! -d .venv ]; then
  echo "-- creating .venv --"
  python3 -m venv .venv
fi
echo "-- installing python deps --"
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -e . >/dev/null

# 3. .env scaffold.
if [ ! -f .env ]; then
  cp .env.example .env
  echo "-- created .env from template (EDIT IT: CONTACT_EMAIL, NODE_ID, NODE_COUNT, BRAVE_API_KEY) --"
fi

cat <<'NEXT'

============================================================
Bootstrap done. Three manual steps remain (see DEPLOY.md):

  1. Edit .env
       nano .env
     Set CONTACT_EMAIL, BRAVE_API_KEY, and this machine's
     NODE_ID / NODE_COUNT (laptop=0, VM1=1, VM2=2, ...; set
     NODE_COUNT to the TOTAL machine count on EVERY machine).

  2. Connect Google Drive (same Google account on every machine):
       rclone config
     new remote -> name it "gdrive" -> storage "drive" -> defaults.
     Headless VM? answer "n" to the browser prompt, run
       rclone authorize "drive"
     on your laptop, and paste the token back.

  3. (Optional, once) seed hashes of your prior collection so they
     are never re-downloaded:
       rclone hashsum sha256 gdrive_old:OldFolder > prior_hashes.txt
       ./.venv/bin/pptxsweeper import-catalog prior_hashes.txt

  Then start it inside tmux:
       tmux new -s sweeper
       ./.venv/bin/pptxsweeper run
     Detach with Ctrl-B then D; reattach with `tmux attach -t sweeper`.
============================================================
NEXT
