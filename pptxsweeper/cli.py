"""pptxsweeper CLI -- one subcommand per pipeline stage, all resumable.

    pptxsweeper harvest --tier N | --source name
    pptxsweeper filter
    pptxsweeper download [--dry-run]
    pptxsweeper classify [--dry-run] [--reclassify]
    pptxsweeper package [--dry-run] [--force] [--loop]
    pptxsweeper status [--sync]
    pptxsweeper import-catalog PATH
    pptxsweeper sync-dedup
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import click

from .config import Config, ConfigError
from .db.dao import Registry
from .logging_setup import setup_logging
from .node import NodeIdentity


def _boot(stage: str) -> tuple[Config, Registry]:
    try:
        cfg = Config.load()
    except ConfigError as exc:
        raise click.ClickException(str(exc))
    cfg.ensure_dirs()
    lg = cfg.raw.get("logging", {})
    setup_logging(stage, cfg.path("paths", "logs_dir"),
                  level=lg.get("level", "INFO"),
                  json_lines=bool(lg.get("json_lines", True)),
                  rotate_max_bytes=int(lg.get("rotate_max_bytes", 50 * 2**20)),
                  rotate_backup_count=int(lg.get("rotate_backup_count", 10)))
    return cfg, Registry(cfg.path("paths", "db_path"))


def _rclone(cfg: Config):
    from .packager.rclone import Rclone
    rc = cfg.raw["rclone"]
    return Rclone(bin=rc["bin"], remote=cfg.rclone_remote(),
                  root_folder=cfg.rclone_root_folder(),
                  retries=int(cfg.raw["upload"]["max_retries"]),
                  retry_backoff_s=list(cfg.raw["upload"]["retry_backoff_s"]))


def _rclone_handoff(cfg: Config):
    """Rclone rooted at the FIXED, shared handoff folder (independent of
    each VM's own delivery root) so producer and consumer meet there."""
    from .packager.rclone import Rclone
    rc = cfg.raw["rclone"]
    root = cfg.raw["multi_node"].get("handoff_root", "PptxSweeper_Handoff")
    return Rclone(bin=rc["bin"], remote=cfg.rclone_remote(), root_folder=root,
                  retries=int(cfg.raw["upload"]["max_retries"]),
                  retry_backoff_s=list(cfg.raw["upload"]["retry_backoff_s"]))


@click.group()
def main() -> None:
    """PptxSweeper: million-scale presentation acquisition pipeline."""


# ----------------------------------------------------------------------
@main.command()
@click.option("--tiers", default="1,2,3,4,6,7",
              help="Comma-separated harvest tiers to loop (default 1,2,3,4,6,7).")
@click.option("--harvest-interval-hours", type=float, default=24,
              help="Re-run discovery this often.")
@click.option("--harvest-limit", type=int, default=None,
              help="Cap candidates per source per pass (for testing).")
@click.option("--with-commoncrawl", is_flag=True,
              help="Also scan the Common Crawl index (slow; near-zero ppt/pptx yield).")
@click.option("--no-harvest", is_flag=True,
              help="Download-only mode: skip discovery (for a consumer VM fed via handoff).")
@click.option("--handoff", type=click.Choice(["producer", "consumer", "none"]),
              default="none",
              help="Enable URL handoff: 'producer' exports a share of discovered "
                   "URLs to Drive; 'consumer' imports them (pair with --no-harvest).")
def run(tiers: str, harvest_interval_hours: float, harvest_limit: int | None,
        with_commoncrawl: bool, no_harvest: bool, handoff: str) -> None:
    """EVERYTHING with one command: find URLs, download, quality-check,
    and continuously upload finished batches to Google Drive. Ctrl+C to
    stop; re-running resumes exactly where it left off."""
    cfg, reg = _boot("run")
    reg.close()
    from .orchestrator import Orchestrator, preflight
    if preflight(cfg) is None:
        raise SystemExit(1)
    tier_list = [int(t) for t in tiers.split(",") if t.strip()]
    orch = Orchestrator(cfg, tier_list, harvest_interval_hours, harvest_limit,
                        with_commoncrawl=with_commoncrawl,
                        no_harvest=no_harvest, handoff_role=handoff)
    print(f"Pipeline running; Ctrl+C to stop. Stage logs: data/logs/*.jsonl\n"
          f"Harvest order: {', '.join(orch.harvest_sources)}")
    orch.run()


# ----------------------------------------------------------------------
@main.command()
@click.option("--tier", type=int, default=None, help="Run all harvesters of a tier.")
@click.option("--source", "source_name", default=None,
              help="Harvester name(s), comma-separated -- run concurrently.")
@click.option("--limit", type=int, default=None, help="Stop each source after N candidates.")
def harvest(tier: int | None, source_name: str | None, limit: int | None) -> None:
    """Run discovery plugins; upsert candidates (dedupe on url)."""
    cfg, reg = _boot("harvest")
    from .harvesters import all_harvesters
    if source_name:
        names = [n.strip() for n in source_name.split(",") if n.strip()]
    elif tier is not None:
        names = sorted(all_harvesters(tier))
        if not names:
            raise click.ClickException(f"no harvesters registered for tier {tier}")
    else:
        raise click.ClickException("pass --tier N or --source NAME "
                                   f"(available: {', '.join(sorted(all_harvesters()))})")
    from .harvesters.base import run_harvesters
    stats = asyncio.run(run_harvesters(cfg, reg, names, limit_per_source=limit))
    click.echo(json.dumps(stats, indent=2))


# ----------------------------------------------------------------------
@main.command("filter")
def filter_cmd() -> None:
    """Pre-download filtering: blocklists, extension sanity, domain caps."""
    cfg, reg = _boot("filter")
    from .stages.filter_stage import run_filter
    click.echo(json.dumps(run_filter(cfg, reg), indent=2))


# ----------------------------------------------------------------------
@main.command()
@click.option("--dry-run", is_flag=True, help="List what would be downloaded; no requests.")
@click.option("--concurrency", type=int, default=None)
def download(dry_run: bool, concurrency: int | None) -> None:
    """Async polite downloader (resumable; SIGTERM-safe)."""
    cfg, reg = _boot("download")
    from .download.worker import DownloadStage
    stage = DownloadStage(cfg, reg, dry_run=dry_run, concurrency=concurrency)
    stats = asyncio.run(stage.run())
    click.echo(json.dumps(stats, indent=2))


# ----------------------------------------------------------------------
@main.command()
@click.option("--dry-run", is_flag=True)
@click.option("--limit", type=int, default=None)
@click.option("--reclassify", is_flag=True,
              help="Re-run decisions from stored feature vectors (no file access).")
@click.option("--sync-review/--no-sync-review", default=True,
              help="Sync review/ dir to Drive _review/ after classifying.")
def classify(dry_run: bool, limit: int | None, reclassify: bool, sync_review: bool) -> None:
    """Validate + convert + quality-classify + compliance-screen."""
    cfg, reg = _boot("classify")
    from .stages.classify_stage import ClassifyStage, run_reclassify
    if reclassify:
        click.echo(json.dumps(run_reclassify(cfg, reg), indent=2))
        return
    stage = ClassifyStage(cfg, reg, dry_run=dry_run)
    stats = stage.run(limit=limit)
    if sync_review and not dry_run and stats.get("review"):
        try:
            stage.sync_review_to_drive(_rclone(cfg))
        except Exception as exc:
            click.echo(f"review sync to Drive failed (will retry next run): {exc}", err=True)
    click.echo(json.dumps(stats, indent=2))


# ----------------------------------------------------------------------
@main.command()
@click.option("--dry-run", is_flag=True)
@click.option("--stream", is_flag=True,
              help="Upload each qualifying file to Drive immediately (no "
                   "waiting for a full batch); folders/manifests stay intact.")
@click.option("--force", is_flag=True,
              help="Close a short batch / ignore the daily upload budget.")
@click.option("--loop", "loop_", is_flag=True,
              help="Keep packaging; sleep and resume when held/budget-exhausted.")
def package(dry_run: bool, stream: bool, force: bool, loop_: bool) -> None:
    """Upload accepted files to Drive (streaming or full-batch mode)."""
    cfg, reg = _boot("package")
    from .packager.package_stage import PackageStage
    stage = PackageStage(cfg, reg, dry_run=dry_run)
    if stream:
        click.echo(json.dumps(stage.stream_upload(), indent=2))
        return
    while True:
        result = stage.run(force=force)
        click.echo(json.dumps(result, indent=2))
        if not loop_ or dry_run:
            break
        if result["status"] in ("finalized",):
            continue        # more supply may be waiting
        if result["status"] == "budget_exhausted":
            time.sleep(3600)    # budget resets at UTC midnight; re-check hourly
            continue
        if result["status"] in ("held", "empty"):
            time.sleep(600)
            continue
        break               # verify_failed / payloads_missing need attention


# ----------------------------------------------------------------------
@main.command()
@click.option("--json", "as_json", is_flag=True, help="Print status.json to stdout.")
@click.option("--sync", is_flag=True, help="Upload status.json to Drive _status/.")
def status(as_json: bool, sync: bool) -> None:
    """Dashboard: counts, acceptance rates, ETA; writes status.json."""
    cfg, reg = _boot("status")
    from .status.dashboard import build_status, print_dashboard, write_status_json, \
        sync_status_to_drive
    st = build_status(cfg, reg)
    path = write_status_json(cfg, st)
    if sync:
        sync_status_to_drive(cfg, path, _rclone(cfg))
    click.echo(json.dumps(st, indent=2, default=str) if as_json else print_dashboard(st))


# ----------------------------------------------------------------------
@main.command("import-catalog")
@click.argument("path", type=click.Path(exists=True))
def import_catalog_cmd(path: str) -> None:
    """Seed the existing SHA256 catalog so those files are never re-downloaded."""
    cfg, reg = _boot("import_catalog")
    from .catalog_import import import_catalog
    click.echo(json.dumps(import_catalog(reg, path), indent=2))


# ----------------------------------------------------------------------
@main.command("promote-review")
@click.option("--stream/--no-stream", default=True,
              help="Also upload the promoted files immediately (default).")
@click.option("--quality-only", is_flag=True,
              help="Keep compliance-flagged (PII/minors/rights) files in review; "
                   "promote only quality-borderline ones.")
@click.option("--from-drive", is_flag=True,
              help="Local payloads gone: promote pending review files by server-side "
                   "moving them from the Drive _review/ folder into the batch (no re-download).")
@click.option("--limit", type=int, default=None, help="Cap number promoted (testing).")
@click.option("--dry-run", is_flag=True, help="Report what would be promoted; no changes.")
def promote_review_cmd(stream: bool, quality_only: bool, from_drive: bool,
                       limit: int | None, dry_run: bool) -> None:
    """Promote manually-approved REVIEW files into the open batch,
    continuing its numbering and writing sidecars. Default reuses the local
    review payloads; --from-drive moves them from Drive _review/ instead."""
    cfg, reg = _boot("promote_review")
    if from_drive:
        from .stages.review_promote import promote_review_from_drive
        stats = promote_review_from_drive(cfg, reg, _rclone(cfg),
                                          node=NodeIdentity.from_env(),
                                          limit=limit, dry_run=dry_run)
        click.echo(json.dumps(stats, indent=2))
        return
    from .stages.review_promote import promote_review
    stats = promote_review(reg, only_quality_borderline=quality_only, dry_run=dry_run)
    click.echo(json.dumps(stats, indent=2))
    if stream and not dry_run and stats.get("promoted"):
        from .packager.package_stage import PackageStage
        result = PackageStage(cfg, reg).stream_upload()
        click.echo(json.dumps(result, indent=2))


# ----------------------------------------------------------------------
@main.command("export-urls")
@click.option("--fraction", type=float, default=0.6, show_default=True,
              help="Share of the discovered backlog to hand to the consumer node.")
@click.option("--out", "out_path", default=None,
              help="Local CSV path (default: data/tmp_downloads/_handoff/<node>_<ts>.csv).")
@click.option("--limit", type=int, default=None, help="Cap exported rows (testing).")
@click.option("--to-drive/--no-to-drive", default=True,
              help="Also upload the CSV to the Drive _handoff/ folder (default).")
def export_urls_cmd(fraction: float, out_path: str | None, limit: int | None,
                    to_drive: bool) -> None:
    """Producer: hand a deterministic fraction of discovered URLs to the
    consumer node (marks them handed-off so THIS node won't download them)."""
    cfg, reg = _boot("export_urls")
    node = NodeIdentity.from_env()
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"node{node.node_id}_{ts}.csv"
    if out_path is None:
        out_path = str(cfg.path("paths", "download_tmp_dir") / "_handoff" / name)
    from .stages.handoff import export_urls
    stats = export_urls(reg, fraction=fraction, out_path=out_path, limit=limit)
    if to_drive and stats["exported"]:
        rc = _rclone_handoff(cfg)
        rc.mkdir()
        rc.copy_file(Path(out_path), dest_name=name)
        stats["drive"] = f"{cfg.raw['multi_node'].get('handoff_root')}/{name}"
    click.echo(json.dumps(stats, indent=2))


@main.command("import-urls")
@click.argument("path", type=click.Path(exists=True), required=False)
@click.option("--from-drive/--no-from-drive", default=True,
              help="Pull handoff CSVs from the Drive _handoff/ folder (default).")
def import_urls_cmd(path: str | None, from_drive: bool) -> None:
    """Consumer: import handed-off URLs into this node's registry."""
    cfg, reg = _boot("import_urls")
    from .stages.handoff import import_urls
    node = NodeIdentity.from_env()
    totals = {"read": 0, "new": 0, "files": 0}
    if path:
        s = import_urls(reg, path)
        totals["read"] += s["read"]; totals["new"] += s["new"]; totals["files"] += 1
    if from_drive:
        rclone = _rclone_handoff(cfg)
        local_dir = cfg.path("paths", "download_tmp_dir") / "_handoff_in"
        local_dir.mkdir(parents=True, exist_ok=True)
        for entry in rclone.lsjson():
            fname = entry.get("Name", "")
            # skip CSVs this node produced itself
            if not fname.endswith(".csv") or fname.startswith(f"node{node.node_id}_"):
                continue
            rclone.download_file((fname,), local_dir)
            s = import_urls(reg, local_dir / fname)
            totals["read"] += s["read"]; totals["new"] += s["new"]; totals["files"] += 1
            rclone.delete_file(fname)   # consumed: don't re-import
            (local_dir / fname).unlink(missing_ok=True)
    click.echo(json.dumps(totals, indent=2))


# ----------------------------------------------------------------------
@main.command("sync-dedup")
def sync_dedup_cmd() -> None:
    """Exchange SHA256 lists with other machines through Drive _dedup/."""
    cfg, reg = _boot("sync_dedup")
    from .dedup_sync import sync
    node = NodeIdentity.from_env()
    result = sync(reg, node, _rclone(cfg),
                  tmp_dir=cfg.path("paths", "download_tmp_dir") / "_dedup",
                  dedup_folder=cfg.raw["multi_node"]["dedup_folder"])
    click.echo(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
