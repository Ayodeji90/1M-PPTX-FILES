"""rclone subprocess wrapper. All Google Drive I/O goes through here.

Rules:
- `copy`, never `move`, until the upload is verified (`check`).
- rclone auto-creates missing remote folders; we still run an idempotent
  `mkdir` at batch open per spec.
- Never mount Drive as a filesystem: downloads land in local temp, get
  validated/classified locally, and only accepted files are uploaded.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger("pptxsweeper.rclone")


class RcloneError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, stderr: str):
        super().__init__(f"rclone failed ({returncode}): {' '.join(cmd)}\n{stderr[-2000:]}")
        self.returncode = returncode
        self.stderr = stderr


class Rclone:
    def __init__(self, bin: str = "rclone", remote: str = "gdrive",
                 root_folder: str = "PptxSweeper_Delivery",
                 retries: int = 5, retry_backoff_s: list[int] | None = None):
        self.bin = bin
        self.remote = remote
        self.root_folder = root_folder
        self.retries = retries
        self.retry_backoff_s = retry_backoff_s or [10, 30, 120, 300, 900]

    # ------------------------------------------------------------------
    def remote_path(self, *parts: str) -> str:
        segments = [self.root_folder, *[p for p in parts if p]]
        return f"{self.remote}:{'/'.join(segments)}"

    def _run(self, args: list[str], retry: bool = True, timeout: int = 3600) -> subprocess.CompletedProcess:
        cmd = [self.bin, *args]
        attempts = self.retries if retry else 1
        last: subprocess.CompletedProcess | None = None
        for attempt in range(attempts):
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if proc.returncode == 0:
                return proc
            last = proc
            log.warning("rclone attempt %d/%d failed: %s", attempt + 1, attempts,
                        proc.stderr.strip()[-500:])
            if attempt < attempts - 1:
                time.sleep(self.retry_backoff_s[min(attempt, len(self.retry_backoff_s) - 1)])
        assert last is not None
        raise RcloneError(cmd, last.returncode, last.stderr)

    # ------------------------------------------------------------------
    def available(self) -> bool:
        try:
            self._run(["version"], retry=False, timeout=30)
            return True
        except (RcloneError, FileNotFoundError):
            return False

    def check_remote_configured(self) -> bool:
        """True if the configured remote exists in rclone config."""
        try:
            proc = self._run(["listremotes"], retry=False, timeout=30)
        except (RcloneError, FileNotFoundError):
            return False
        return f"{self.remote}:" in proc.stdout.split()

    def mkdir(self, *parts: str) -> None:
        self._run(["mkdir", self.remote_path(*parts)])

    def copy_file(self, local: Path, *remote_parts: str,
                  dest_name: str | None = None) -> None:
        """Upload one file into a remote folder (copy, never move)."""
        self._run(["copyto", str(local),
                   self.remote_path(*remote_parts, dest_name or local.name)])

    def stat_file(self, *remote_parts: str) -> dict | None:
        """Metadata for one remote file ({Size, Name, ...}) or None."""
        try:
            proc = self._run(["lsjson", self.remote_path(*remote_parts)], retry=False)
        except RcloneError:
            return None
        entries = json.loads(proc.stdout or "[]")
        return entries[0] if entries else None

    def copy_dir(self, local_dir: Path, *remote_parts: str,
                 bwlimit: str | None = None) -> None:
        args = ["copy", str(local_dir), self.remote_path(*remote_parts),
                "--transfers", "8", "--checkers", "16"]
        if bwlimit:
            args += ["--bwlimit", bwlimit]
        self._run(args)

    def check(self, local_dir: Path, *remote_parts: str, method: str = "size-only") -> bool:
        """Verify local_dir contents exist identically on the remote."""
        args = ["check", str(local_dir), self.remote_path(*remote_parts), "--one-way"]
        if method == "size-only":
            args.append("--size-only")
        try:
            self._run(args, retry=False)
            return True
        except RcloneError:
            return False

    def lsjson(self, *remote_parts: str) -> list[dict]:
        """List a remote folder; [] if it does not exist."""
        try:
            proc = self._run(["lsjson", self.remote_path(*remote_parts)], retry=False)
        except RcloneError as exc:
            if "directory not found" in exc.stderr.lower():
                return []
            raise
        return json.loads(proc.stdout or "[]")

    def download_file(self, remote_parts: tuple[str, ...], local_dir: Path) -> None:
        self._run(["copy", self.remote_path(*remote_parts), str(local_dir)])

    def delete_file(self, *remote_parts: str) -> None:
        self._run(["deletefile", self.remote_path(*remote_parts)])
