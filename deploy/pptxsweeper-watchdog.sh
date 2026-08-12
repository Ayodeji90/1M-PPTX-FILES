#!/usr/bin/env bash
# PptxSweeper watchdog -- keeps the 24/7 VM pipeline self-healing.
#
# Runs every 10 minutes (see pptxsweeper-watchdog.timer). Liveness model:
#   - The orchestrator PARENT prints a status line to the journal every
#     15s, so "any journal line in the last 5 min" proves the parent is
#     alive.
#   - Stage subprocesses log to data/logs/*.jsonl (their stdout/stderr
#     are DEVNULL'd), so stage activity is visible as FRESH mtimes on
#     those files -- including deliberate pauses ("free disk below...",
#     "backlog >= cap; pausing"), which also write to download.jsonl.
#   - The registry's MAX(updated_at) is the final heartbeat.
#
# Restart decision:
#   - service inactive                        -> start it
#   - no journal line in 5 min (parent dead)  -> restart
#   - any stage log touched in 10 min         -> healthy (working/paused)
#   - DB write within 30 min                  -> healthy
#   - otherwise                               -> wedged -> restart
#
# A wedged pipeline (hung download stage, stuck classify pass, dead
# upload loop) otherwise sits doing nothing forever on an unattended VM.
# Rows are crash-safe (WAL sqlite, resumable stages): a restart loses
# nothing but the stuck time.
#
# Install:
#   sudo cp deploy/pptxsweeper-watchdog.service deploy/pptxsweeper-watchdog.timer /etc/systemd/system/
#   sudo cp deploy/pptxsweeper-watchdog.sh /usr/local/bin/
#   sudo chmod +x /usr/local/bin/pptxsweeper-watchdog.sh
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now pptxsweeper-watchdog.timer
#   journalctl -u pptxsweeper-watchdog.service -f   # see what it does
set -u

SERVICE=pptxsweeper
REPO_DIR="${1:-/home/azureuser/1M-PPTX-FILES}"
DB="$REPO_DIR/data/registry.db"
LOG_DIR="$REPO_DIR/data/logs"
HEARTBEAT_MAX_MIN="${HEARTBEAT_MAX_MIN:-30}"
LOG=/tmp/pptxsweeper-watchdog.log
PARENT_WINDOW_MIN=5
STAGE_WINDOW_MIN=10

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# --- 1. service not running -> start it ---------------------------------
active=$(systemctl is-active "$SERVICE" 2>/dev/null || echo inactive)
if [ "$active" != "active" ]; then
    log "service '$SERVICE' is $active; starting"
    systemctl start "$SERVICE"
    exit 0
fi

# --- 2. parent wedged/dead: no journal line at all in the window --------
if ! journalctl -u "$SERVICE" --since "${PARENT_WINDOW_MIN} min ago" --no-pager \
       2>/dev/null | grep -q .; then
    log "no journal output in ${PARENT_WINDOW_MIN}m (parent wedged); restarting '$SERVICE'"
    systemctl restart "$SERVICE"
    exit 0
fi

# --- 3. stage activity: any stage log touched recently -> healthy -------
# (covers working stages AND deliberate disk/backlog pauses, both of
# which write to download.jsonl)
recent_stage_log=""
for f in "$LOG_DIR"/download.jsonl "$LOG_DIR"/classify.jsonl \
         "$LOG_DIR"/package.jsonl "$LOG_DIR"/filter.jsonl; do
    if [ -f "$f" ] && [ $(( $(date +%s) - $(stat -c %Y "$f") )) -le $(( STAGE_WINDOW_MIN * 60 )) ]; then
        recent_stage_log="$f"
        break
    fi
done
if [ -n "$recent_stage_log" ]; then
    log "healthy: stage log $recent_stage_log active within ${STAGE_WINDOW_MIN}m"
    exit 0
fi

# --- 4. final wedge check: no DB write for HEARTBEAT_MAX_MIN -------------
age=$(python3 - "$DB" <<'PY'
import sqlite3, sys, time
db = sys.argv[1]
try:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    last = c.execute("SELECT MAX(updated_at) FROM urls").fetchone()[0]
    c.close()
except Exception:
    last = None
if not last:
    import os
    last = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(os.path.getmtime(db)))
last = last.replace("Z", "").split(".")[0]
try:
    ts = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S"))
except Exception:
    ts = 0
print(int(time.time() - ts))
PY
)

if [ -n "$age" ] && [ "$age" -gt $(( HEARTBEAT_MAX_MIN * 60 )) ]; then
    log "no DB write for ${age}s (>${HEARTBEAT_MAX_MIN}m) with no stage activity; restarting '$SERVICE'"
    systemctl restart "$SERVICE"
    exit 0
fi

log "healthy: last DB write ${age}s ago"
exit 0
