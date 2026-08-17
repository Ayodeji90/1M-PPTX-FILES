"""Headless LibreOffice conversions: legacy .ppt -> .pptx, and deck -> PDF
(for image extraction). Sandboxed subprocess with a hard timeout and a
per-call user profile (-env:UserInstallation) so parallel conversions
don't fight over the shared LibreOffice profile lock. Any failure ->
REJECT upstream.
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


def _soffice(src: Path, out_dir: Path, convert_to: str, out_ext: str,
             soffice_bin: str, timeout_s: int) -> ConversionResult:
    """One sandboxed soffice conversion: `src` -> `out_dir/name.{out_ext}`.

    Shared by .ppt->.pptx (classify) and .pptx/.ppt->.pdf (extract).
    LibreOffice writes per-document scratch files (lu*.tmp) to TMPDIR.
    Default that to /tmp and a killed/crashed conversion (our own timeout
    path SIGKILLs the process group) leaks them there forever; they
    accumulate until they eat the download disk floor and silently pause
    all downloads. Redirect TMPDIR to a per-call dir that
    TemporaryDirectory removes on every exit path, so nothing escapes.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="soffice_profile_") as profile_dir, \
         tempfile.TemporaryDirectory(prefix="soffice_tmp_") as soffice_tmp:
        env = {**os.environ, "TMPDIR": soffice_tmp,
               "TMP": soffice_tmp, "TEMP": soffice_tmp}
        cmd = [
            soffice_bin, "--headless", "--norestore", "--nolockcheck",
            f"-env:UserInstallation=file://{profile_dir}/{uuid.uuid4().hex}",
            "--convert-to", convert_to, "--outdir", str(out_dir), str(src),
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    env=env, start_new_session=True)
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
    expected = out_dir / (src.stem + "." + out_ext)
    if not expected.exists() or expected.stat().st_size == 0:
        return ConversionResult(False, reason="soffice produced no output file")
    return ConversionResult(True, output_path=expected)


def convert_ppt_to_pptx(ppt_path: str | Path, out_dir: str | Path,
                        soffice_bin: str = "soffice", timeout_s: int = 120) -> ConversionResult:
    ppt_path = Path(ppt_path)
    return _soffice(ppt_path, Path(out_dir), "pptx", "pptx", soffice_bin, timeout_s)


def convert_to_pdf(deck_path: str | Path, out_dir: str | Path,
                   soffice_bin: str = "soffice", timeout_s: int = 180) -> ConversionResult:
    """Render a .pptx/.ppt deck to PDF (the first step of page extraction)."""
    deck_path = Path(deck_path)
    return _soffice(deck_path, Path(out_dir), "pdf", "pdf", soffice_bin, timeout_s)
