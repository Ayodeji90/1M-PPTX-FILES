"""Status dashboard: human table + machine-readable status.json."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Config
from ..db.dao import Registry, utcnow
from ..node import NodeIdentity

log = logging.getLogger("pptxsweeper.status")

TARGET_ACCEPTED = 1_000_000


def build_status(cfg: Config, reg: Registry) -> dict:
    node = NodeIdentity.from_env()
    now = datetime.now(timezone.utc)

    by_status = {r["status"]: r["n"] for r in reg.conn.execute(
        "SELECT status, COUNT(*) n FROM urls GROUP BY status")}
    by_tier_status = [dict(r) for r in reg.conn.execute(
        "SELECT tier, status, COUNT(*) n FROM urls GROUP BY tier, status")]
    by_quality = {r["quality"] or "unclassified": r["n"] for r in reg.conn.execute(
        "SELECT quality, COUNT(*) n FROM files GROUP BY quality")}

    # Acceptance rate per tier per 1k candidates
    tier_stats = {}
    for r in reg.conn.execute(
        """SELECT u.tier,
                  COUNT(*) AS candidates,
                  SUM(CASE WHEN f.decision='DELIVER' THEN 1 ELSE 0 END) AS accepted,
                  SUM(COALESCE(u.content_length,0)) AS bytes_downloaded
           FROM urls u LEFT JOIN files f ON f.url_id = u.id
           GROUP BY u.tier"""):
        candidates = r["candidates"] or 0
        accepted = r["accepted"] or 0
        tier_stats[str(r["tier"])] = {
            "candidates": candidates,
            "accepted": accepted,
            "accepted_per_1k_candidates": round(1000 * accepted / candidates, 2) if candidates else 0.0,
            "bytes_downloaded": r["bytes_downloaded"] or 0,
            "bytes_per_accepted": round((r["bytes_downloaded"] or 0) / accepted) if accepted else None,
        }

    batches = [dict(r) for r in reg.conn.execute(
        "SELECT batch_id, folder_name, state, file_count, high_count, medium_count, "
        "composition_ok, finalized_at FROM batches ORDER BY batch_id")]
    finalized = [b for b in batches if b["state"] == "finalized"]
    delivered_total = sum(b["file_count"] for b in finalized)

    # Blocking incidents
    incidents = {r["kind"]: r["n"] for r in reg.conn.execute(
        "SELECT kind, COUNT(*) n FROM events GROUP BY kind")}
    parked = reg.conn.execute(
        "SELECT COUNT(*) FROM domains WHERE state='parked'").fetchone()[0]
    blacklisted = reg.conn.execute(
        "SELECT COUNT(*) FROM domains WHERE state='blacklisted'").fetchone()[0]

    # ETA math: accepted files per hour over the last 24h -> hours to target
    day_ago = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    accepted_24h = reg.conn.execute(
        "SELECT COUNT(*) FROM files WHERE decision='DELIVER' AND created_at >= ?",
        (day_ago,)).fetchone()[0]
    rate_per_day = accepted_24h  # this node only
    remaining = max(0, TARGET_ACCEPTED - delivered_total)
    eta_days = round(remaining / (rate_per_day * node.node_count), 1) \
        if rate_per_day else None

    return {
        "generated_at": utcnow(),
        "node": {"id": node.node_id, "count": node.node_count},
        "target_accepted": TARGET_ACCEPTED,
        "urls_by_status": by_status,
        "urls_by_tier_status": by_tier_status,
        "files_by_quality": by_quality,
        "tier_stats": tier_stats,
        "batches": batches,
        "batches_finalized": len(finalized),
        "files_delivered": delivered_total,
        "upload_budget_used_today_bytes": reg.budget_used_today(),
        "blocking": {"events": incidents, "domains_parked": parked,
                     "domains_blacklisted": blacklisted},
        "accepted_last_24h_this_node": accepted_24h,
        "eta_days_at_current_rate_all_nodes": eta_days,
    }


def write_status_json(cfg: Config, status: dict) -> Path:
    status_dir = cfg.path("paths", "status_dir")
    status_dir.mkdir(parents=True, exist_ok=True)
    node = status["node"]["id"]
    out = status_dir / f"status_node{node}.json"
    out.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
    return out


def sync_status_to_drive(cfg: Config, status_path: Path, rclone) -> None:
    folder = cfg.raw["rclone"]["status_folder"]
    rclone.mkdir(folder)
    rclone.copy_file(status_path, folder)


def print_dashboard(status: dict) -> str:
    lines = []
    add = lines.append
    add(f"PptxSweeper status  (node {status['node']['id']+1}/{status['node']['count']}, "
        f"generated {status['generated_at']})")
    add("=" * 72)
    add(f"Delivered: {status['files_delivered']:,} / {status['target_accepted']:,}  "
        f"| batches finalized: {status['batches_finalized']}")
    if status["eta_days_at_current_rate_all_nodes"]:
        add(f"ETA at current rate (all nodes): {status['eta_days_at_current_rate_all_nodes']} days")
    add("")
    add("URLs by status:")
    for k, v in sorted(status["urls_by_status"].items(), key=lambda x: -x[1]):
        add(f"  {k:<14} {v:>12,}")
    add("")
    add("Files by quality: " + ", ".join(
        f"{k}={v:,}" for k, v in status["files_by_quality"].items()))
    add("")
    add("Tier acceptance (accepted per 1k candidates):")
    for tier, s in sorted(status["tier_stats"].items()):
        bpa = f"{s['bytes_per_accepted']:,}B/file" if s["bytes_per_accepted"] else "-"
        add(f"  tier {tier}: {s['accepted']:>8,} / {s['candidates']:>10,}  "
            f"({s['accepted_per_1k_candidates']:>7.1f}/1k)  bandwidth {bpa}")
    add("")
    blocking = status["blocking"]
    add(f"Blocking: parked={blocking['domains_parked']} "
        f"blacklisted={blocking['domains_blacklisted']} events={blocking['events']}")
    budget_gb = status["upload_budget_used_today_bytes"] / 1024 ** 3
    add(f"Upload budget used today: {budget_gb:.1f} GB")
    return "\n".join(lines)
