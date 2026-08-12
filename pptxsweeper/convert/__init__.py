"""Legacy .ppt -> .pptx conversion via headless LibreOffice.

Sandboxed subprocess with a hard timeout and a per-call user profile
(-env:UserInstallation) so parallel conversions don't fight over the
shared LibreOffice profile lock. Any failure -> REJECT upstream.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pptxsweeper.convert")


@dataclass
class ConversionResult:
    ok: bool
    output_path: Path | None = None
    reason: str = ""


def convert_ppt_to_pptx(ppt_path: str | Path, out_dir: str | Path,
                        soffice_bin: str = "soffice", timeout_s: int = 120) -> ConversionResult:
    ppt_path = Path(ppt_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="soffice_profile_") as profile_dir:
        cmd = [
            soffice_bin, "--headless", "--norestore", "--nolockcheck",
            f"-env:UserInstallation=file://{profile_dir}/{uuid.uuid4().hex}",
            "--convert-to", "pptx", "--outdir", str(out_dir), str(ppt_path),
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
        except FileNotFoundError:
            return ConversionResult(False, reason=f"{soffice_bin} not installed")
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # LibreOffice daemonizes and its children inherit the pipe FDs,
            # so killing only the direct child leaves the pipes open and
            # communicate() would block FOREVER -- the exact wedge that
            # froze classify (0% CPU everywhere, run never finished). Kill
            # the whole process group (start_new_session=True above) so the
            # pipes close and we return promptly.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                proc.communicate()
            except Exception:
                pass
            return ConversionResult(False, reason=f"soffice timeout after {timeout_s}s")

    if proc.returncode != 0:
        return ConversionResult(False, reason=f"soffice exit {proc.returncode}: "
                                              f"{(stderr or stdout).strip()[:300]}")
    expected = out_dir / (ppt_path.stem + ".pptx")
    if not expected.exists() or expected.stat().st_size == 0:
        return ConversionResult(False, reason="soffice produced no output file")
    return ConversionResult(True, output_path=expected)
