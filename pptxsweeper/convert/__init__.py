"""Legacy .ppt -> .pptx conversion via headless LibreOffice.

Sandboxed subprocess with a hard timeout and a per-call user profile
(-env:UserInstallation) so parallel conversions don't fight over the
shared LibreOffice profile lock. Any failure -> REJECT upstream.
"""
from __future__ import annotations

import logging
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
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return ConversionResult(False, reason=f"soffice timeout after {timeout_s}s")
        except FileNotFoundError:
            return ConversionResult(False, reason=f"{soffice_bin} not installed")

    if proc.returncode != 0:
        return ConversionResult(False, reason=f"soffice exit {proc.returncode}: "
                                              f"{(proc.stderr or proc.stdout).strip()[:300]}")
    expected = out_dir / (ppt_path.stem + ".pptx")
    if not expected.exists() or expected.stat().st_size == 0:
        return ConversionResult(False, reason="soffice produced no output file")
    return ConversionResult(True, output_path=expected)
