"""One-command pipeline: `pptxsweeper run`.

Runs everything continuously in parallel until stopped (Ctrl+C):

  thread A: harvest (all configured tiers) ......... finds URLs, repeats daily
  thread B: filter + download ...................... downloads ppt/pptx locally
  thread C: classify + package + status ............ quality-checks and keeps
                                                     uploading finished batches
                                                     to Google Drive

Each stage runs as a subprocess of the same CLI, so crash-safety,
resume, politeness and locking behave exactly as when stages are run
by hand. Stage output goes to data/logs/*.jsonl; the console shows a
one-line progress summary every minute.
"""
from __future__ import annotations

import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .config import Config
from .node import NodeIdentity
from .packager.rclone import Rclone
from .utils.disk import free_gb

RCLONE_HELP = """\
Google Drive is not connected yet (rclone remote '{remote}' not found).

One-time setup on THIS machine:
  1. Run:  rclone config
  2. Choose: n (new remote), name it: {remote}, storage type: drive
  3. Leave client_id/secret empty, scope: 1 (full access), accept defaults
  4. When asked "Use web browser to automatically authenticate?":
       - on this computer: say y and log into your Google account
       - on a cloud VM (no browser): say n, then on your laptop run
         `rclone authorize "drive"` and paste the token back
  5. Done. Re-run:  pptxsweeper run
"""


# Sources ordered by expected .ppt/.pptx yield per hour. Common Crawl is
# LAST and opt-in: verified to contain ~zero PowerPoint files while
# costing days of index scanning -- never let it starve the others.
HARVEST_PRIORITY = [
    "wayback_cdx",            # tier 1 -- the ppt/pptx workhorse
    "brave_search",           # tier 7 -- open-web filetype dorking: biggest new vein
    "standards_bodies",       # tier 7 -- 3GPP/IETF/OASIS autoindex: near-pure ppt
    "internet_archive",       # tier 6 -- huge directly-downloadable corpus
    "govdata_ckan",           # tier 7 -- national/EU open-data portals
    "tier4_international",    # tier 4
    "us_federal_sitemaps",    # tier 3
    "universities_ocw",       # tier 5
    "zenodo",                 # tier 6
    "figshare",               # tier 6
    "osf",                    # tier 6
    "investor_relations",     # tier 2 -- IR decks are mostly pdf: low ppt yield
    "commoncrawl",            # tier 1 -- opt-in via --with-commoncrawl
    # NOTE: sec_edgar + github_code_search are unregistered (yield ~0 ppt);
    # see their harvester modules for why.
]


class Orchestrator:
    def __init__(self, cfg: Config, tiers: list[int], harvest_interval_h: float,
                 harvest_limit: int | None = None, with_commoncrawl: bool = False,
                 no_harvest: bool = False, handoff_role: str = "none"):
        self.cfg = cfg
        self.harvest_interval_s = harvest_interval_h * 3600
        self.harvest_limit = harvest_limit
        self.no_harvest = no_harvest
        self.handoff_role = handoff_role   # 'producer' | 'consumer' | 'none'
        mn = cfg.raw.get("multi_node", {})
        self.handoff_interval_s = float(mn.get("handoff_interval_hours", 10)) * 3600
        self.handoff_fraction = float(mn.get("handoff_fraction", 0.6))
        self.stop = threading.Event()
        self._children: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self.db_path = cfg.path("paths", "db_path")
        self.current_harvest = "-"

        from .harvesters import all_harvesters
        registered = all_harvesters()
        wanted = [n for n in HARVEST_PRIORITY
                  if n in registered and registered[n].tier in tiers]
        if not with_commoncrawl and "commoncrawl" in wanted:
            wanted.remove("commoncrawl")
        # anything registered but not in the priority list runs at the end
        wanted += [n for n, c in sorted(registered.items())
                   if n not in wanted and c.tier in tiers
                   and (with_commoncrawl or n != "commoncrawl")]
        self.harvest_sources = wanted

    # ------------------------------------------------------------------
    def _disk_ok(self) -> bool:
        """OS-level backstop: refuse to run ANY stage when free disk on
        the data dir is below disk.hard_min_free_gb.

        The download stage has its own finer guard
        (download.min_free_disk_gb) that only protects tmp_downloads;
        staging/, batch_build/, review/ and logs are otherwise unguarded
        -- so a stalled Drive upload (quota/auth/storage-full) would let
        those dirs fill the disk until every process, sshd included,
        wedges in D-state and the VM becomes unreachable. This check
        turns that cascade into a visible, recoverable pause."""
        free = free_gb(self.cfg.path("paths", "data_dir"))
        hard_min = float(self.cfg.raw.get("disk", {}).get("hard_min_free_gb", 2))
        if free < hard_min:
            print(f"[{time.strftime('%H:%M:%S')}] WARNING: free disk {free:.1f}GB below "
                  f"{hard_min:.0f}GB hard floor -- pausing all stages. Check Drive "
                  f"uploads (rclone about gdrive:) and review/ dir.", flush=True)
            return False
        return True

    def _reclaim_disk(self) -> None:
        """Cheap, safe disk reclamation before each download cycle:
        stale *.part files, orphaned final payloads (a crash between
        os.replace and the status update leaves a sha256.* file with no
        live url row), and old rotated logs. Payloads referenced by a
        live registry row are never touched here."""
        max_age = float(self.cfg.raw.get("disk", {}).get("reclaim_max_age_h", 24)) * 3600
        cutoff = time.time() - max_age
        reclaimed = 0
        tmp = self.cfg.path("paths", "download_tmp_dir")
        if tmp.is_dir():
            live = set()
            try:
                live = {r[0] for r in self._counts_conn().execute(
                    "SELECT sha256 FROM urls WHERE status IN "
                    "('downloaded','downloading','classified') AND sha256 IS NOT NULL")}
            except sqlite3.Error:
                pass
            for p in tmp.iterdir():
                try:
                    if p.stat().st_mtime >= cutoff:
                        continue
                    if p.suffix == ".part":
                        p.unlink()
                        reclaimed += 1
                    elif live and p.name.split(".", 1)[0] not in live:
                        # final payload whose url row left the live set
                        p.unlink()
                        reclaimed += 1
                except OSError:
                    pass
        # LibreOffice scratch dirs (lu*.tmp) leaked into the system temp
        # dir by killed/crashed conversions. convert/ now redirects TMPDIR
        # into a per-call dir, but pre-fix runs -- and anything that
        # bypasses that path -- leave lu*.tmp here; sweep them so they
        # can't eat the download disk floor and pause the pipeline again.
        try:
            for p in Path(tempfile.gettempdir()).glob("lu*.tmp"):
                try:
                    if p.stat().st_mtime >= cutoff:
                        continue
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink()
                    reclaimed += 1
                except OSError:
                    pass
        except OSError:
            pass
        logs = self.cfg.path("paths", "logs_dir")
        if logs.is_dir():
            def _is_rotated(p: Path) -> bool:
                # RotatingFileHandler names: base.jsonl.1, .2, ... or .gz
                return p.suffix == ".gz" or (
                    len(p.suffix) > 1 and p.suffix[1:].isdigit())
            rotated = sorted((p for p in logs.iterdir() if _is_rotated(p)),
                             key=lambda p: p.stat().st_mtime)
            # drop old rotated logs beyond the 5 newest, age-gated
            for p in rotated[:-5]:
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                        reclaimed += 1
                except OSError:
                    pass
        if reclaimed:
            print(f"[{time.strftime('%H:%M:%S')}] reclaimed {reclaimed} stale "
                  f"file(s) to free disk space", flush=True)

    def _counts_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path), timeout=5)

    def _stage(self, name: str, *args: str) -> int:
        """Run one CLI stage as a child process; output goes to its log file."""
        if self.stop.is_set():
            return 1
        proc = subprocess.Popen(
            [sys.executable, "-m", "pptxsweeper.cli", *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with self._lock:
            self._children[name] = proc
        try:
            return proc.wait()
        finally:
            with self._lock:
                self._children.pop(name, None)

    def _sleep(self, seconds: float) -> None:
        self.stop.wait(timeout=seconds)

    # ------------------------------------------------------------------
    def harvest_loop(self) -> None:
        while not self.stop.is_set():
            # all sources discover CONCURRENTLY in one process; a slow
            # sitemap walk can no longer starve the productive sources
            self.current_harvest = f"{len(self.harvest_sources)} sources in parallel"
            args = ["harvest", "--source", ",".join(self.harvest_sources)]
            if self.harvest_limit:
                args += ["--limit", str(self.harvest_limit)]
            self._stage("harvest", *args)
            self.current_harvest = "idle"
            self._sleep(self.harvest_interval_s)

    def download_loop(self) -> None:
        while not self.stop.is_set():
            self._reclaim_disk()
            if not self._disk_ok():
                # hard floor: stop PRODUCING so the disk can never fill.
                # deliver_loop is deliberately NOT gated -- classify's
                # review-prune and package's upload-and-delete are the only
                # mechanisms that FREE disk, and gating them would deadlock
                # recovery (disk low -> nothing drains -> disk stays low).
                self._sleep(300)
                continue
            self._stage("filter", "filter")
            code = self._stage("download", "download")
            self._sleep(120 if code == 0 else 300)

    def deliver_loop(self) -> None:
        node = NodeIdentity.from_env()
        last_dedup = 0.0
        last_promote = 0.0
        promote_interval_s = float(self.cfg.raw.get("classify", {})
                                   .get("review_auto_promote_hours", 2)) * 3600
        while not self.stop.is_set():
            self._stage("classify", "classify")
            # Image delivery: extract graphical pages -> PNG before packing.
            # In deck mode this is a no-op (extract finds no classified
            # files without pages... it finds DELIVER files but the pages
            # table stays empty; gate on config so deck VMs skip the stage).
            if bool(self.cfg.raw.get("delivery", {}).get("image", False)):
                self._stage("extract", "extract")
            # Auto-promote manually-approved review backlog on an interval;
            # the package --stream below then delivers them this cycle.
            if promote_interval_s > 0 and time.time() - last_promote > promote_interval_s:
                self._stage("promote-review", "promote-review", "--no-stream")
                last_promote = time.time()
            # streaming: every qualifying file goes to Drive immediately
            self._stage("package", "package", "--stream")
            self._stage("status", "status", "--sync")
            if node.node_count > 1 and time.time() - last_dedup > 3600:
                self._stage("sync-dedup", "sync-dedup")
                last_dedup = time.time()
            self._sleep(15)

    # ------------------------------------------------------------------
    def _counts(self) -> dict:
        try:
            # not mode=ro: a read-only handle can fail WAL recovery while
            # subprocesses are writing, which would fake all-zero stats
            conn = sqlite3.connect(str(self.db_path), timeout=5)
            by_status = dict(conn.execute(
                "SELECT status, COUNT(*) FROM urls GROUP BY status").fetchall())
            delivered = conn.execute(
                "SELECT COUNT(*) FROM files WHERE delivered_at IS NOT NULL").fetchone()[0]
            batches = conn.execute(
                "SELECT COUNT(*) FROM batches WHERE state='finalized'").fetchone()[0]
            conn.close()
            return {"s": by_status, "delivered": delivered, "batches": batches}
        except sqlite3.Error:
            return {"s": {}, "delivered": 0, "batches": 0}

    def handoff_loop(self) -> None:
        """Producer exports a share of discovered URLs to Drive; consumer
        imports handed-off URLs from Drive. Runs on handoff_interval."""
        # Consumer imports promptly on startup so it has work immediately;
        # producer waits one interval so a backlog has accumulated.
        if self.handoff_role == "producer":
            self._sleep(self.handoff_interval_s)
        while not self.stop.is_set():
            if self.handoff_role == "producer":
                self._stage("export-urls", "export-urls",
                            "--fraction", str(self.handoff_fraction))
            elif self.handoff_role == "consumer":
                self._stage("import-urls", "import-urls")
            self._sleep(self.handoff_interval_s)

    def monitor_loop(self) -> None:
        while not self.stop.is_set():
            c = self._counts()
            s = c["s"]
            print(f"[{time.strftime('%H:%M:%S')}] harvesting={self.current_harvest} "
                  f"queue={s.get('discovered', 0)} "
                  f"downloaded={s.get('downloaded', 0)} staged={s.get('classified', 0)} "
                  f"review={s.get('review', 0)} rejected={s.get('rejected', 0)} "
                  f"UPLOADED-TO-DRIVE={c['delivered']} batches={c['batches']}",
                  flush=True)
            self._sleep(15)

    # ------------------------------------------------------------------
    def shutdown(self, *_args) -> None:
        if self.stop.is_set():
            return
        print("\nStopping (letting in-flight work finish)...", flush=True)
        self.stop.set()
        with self._lock:
            for proc in self._children.values():
                proc.terminate()

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
        threads = [
            threading.Thread(target=self.download_loop, name="download", daemon=True),
            threading.Thread(target=self.deliver_loop, name="deliver", daemon=True),
            threading.Thread(target=self.monitor_loop, name="monitor", daemon=True),
        ]
        if not self.no_harvest:
            threads.insert(0, threading.Thread(target=self.harvest_loop,
                                               name="harvest", daemon=True))
        if self.handoff_role in ("producer", "consumer"):
            threads.append(threading.Thread(target=self.handoff_loop,
                                            name="handoff", daemon=True))
        for t in threads:
            t.start()
        while not self.stop.is_set():
            time.sleep(1)
        # give children a moment to exit gracefully
        deadline = time.time() + 60
        with self._lock:
            children = list(self._children.values())
        for proc in children:
            try:
                proc.wait(timeout=max(1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                proc.kill()
        print("Stopped. Progress is saved; run `pptxsweeper run` to continue.", flush=True)


def preflight(cfg: Config) -> Rclone | None:
    """Verify Google Drive is reachable and create the delivery folder."""
    rc_cfg = cfg.raw["rclone"]
    rclone = Rclone(bin=rc_cfg["bin"], remote=cfg.rclone_remote(),
                    root_folder=cfg.rclone_root_folder(),
                    timeout=int(rc_cfg.get("timeout_s", 900)))
    if not rclone.available():
        print("ERROR: rclone is not installed. Install it:  sudo apt install rclone")
        return None
    if not rclone.check_remote_configured():
        print(RCLONE_HELP.format(remote=cfg.rclone_remote()))
        return None
    rclone.mkdir()  # creates PptxSweeper_Delivery/ on Drive right away
    print(f"Google Drive connected. Delivering to: {rclone.remote_path()}")
    return rclone
