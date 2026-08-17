"""Page rendering for image delivery: deck/PDF -> PNG per selected page.

Pipeline per file:
  .pptx/.ppt -> soffice --convert-to pdf   (reuses convert.convert_to_pdf,
               inheriting its per-call profile + TMPDIR isolation + timeout)
  pdf        -> pdftoppm -png -f N -l N -r {dpi}   (one page per call)

Renders are page-accurate and lossless (original form only -- no
cropping, no element removal). DPI is config (default 150).
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..convert import ConversionResult, convert_to_pdf

log = logging.getLogger("pptxsweeper.extract.render")


@dataclass
class RenderResult:
    ok: bool
    pages: dict[int, Path] = field(default_factory=dict)   # page_index -> PNG
    reason: str = ""


def _pdftoppm(pdf_path: Path, page_index: int, out_dir: Path, dpi: int,
              pdftoppm_bin: str = "pdftoppm", timeout_s: int = 120) -> Path | None:
    """Render ONE page (1-based) to PNG. Returns the PNG path or None."""
    # pdftoppm writes `{prefix}-{page_index}.png` when -f == -l.
    prefix = out_dir / f"page_{page_index}"
    cmd = [pdftoppm_bin, "-png", "-f", str(page_index), "-l", str(page_index),
           "-r", str(dpi), str(pdf_path), str(prefix)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
    except FileNotFoundError:
        log.error("%s not installed; cannot render pages", pdftoppm_bin)
        return None
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
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
        log.error("pdftoppm timeout rendering page %d of %s", page_index, pdf_path.name)
        return None
    if proc.returncode != 0:
        log.error("pdftoppm exit %d for page %d of %s: %s",
                  proc.returncode, page_index, pdf_path.name,
                  (stderr or stdout).strip()[:300])
        return None
    # pdftoppm zero-pads the page number to the width of the last page
    # (page_2-02.png when the pdf has 10+ pages). Match whatever it wrote.
    for cand in out_dir.glob(f"{prefix.name}-*.png"):
        if cand.stat().st_size > 0:
            return cand
    log.error("pdftoppm produced no output for page %d of %s", page_index, pdf_path.name)
    return None


def render_file_pages(payload: Path, page_indexes: list[int], out_dir: Path,
                      dpi: int = 150, soffice_bin: str = "soffice",
                      pdftoppm_bin: str = "pdftoppm",
                      conv_timeout_s: int = 180,
                      page_timeout_s: int = 120) -> RenderResult:
    """Render the given 0-based page indexes of a .pptx/.ppt/.pdf to PNG.

    Returns {ok, pages: {0-based index: PNG path}, reason}. A single page
    failing (corrupt PDF, pdftoppm error) marks the whole file failed --
    partial renders are deleted so a re-run re-renders cleanly.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = Path(payload)
    work = out_dir / f"src_{payload.stem}"
    work.mkdir(parents=True, exist_ok=True)

    if payload.suffix.lower() == ".pdf":
        pdf_path = payload
    else:
        res: ConversionResult = convert_to_pdf(payload, work, soffice_bin=soffice_bin,
                                               timeout_s=conv_timeout_s)
        if not res.ok:
            return RenderResult(False, reason=f"deck_to_pdf_failed:{res.reason}")
        pdf_path = res.output_path

    pages: dict[int, Path] = {}
    try:
        for idx in page_indexes:
            png = _pdftoppm(pdf_path, idx + 1, work, dpi,
                            pdftoppm_bin=pdftoppm_bin, timeout_s=page_timeout_s)
            if png is None:
                return RenderResult(False, pages=pages,
                                    reason=f"pdftoppm_failed_page_{idx + 1}")
            pages[idx] = png
        return RenderResult(True, pages=pages)
    finally:
        if not pages:
            shutil.rmtree(work, ignore_errors=True)
