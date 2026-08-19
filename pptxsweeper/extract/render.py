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
    skipped: list[int] = field(default_factory=list)       # indexes beyond the PDF's real page count
    failed: list[int] = field(default_factory=list)        # indexes that errored rendering
    reason: str = ""


def _pdf_page_count(pdf_path: Path, pdfinfo_bin: str = "pdfinfo") -> int | None:
    """Real page count of the converted PDF via pdfinfo (poppler-utils).

    LibreOffice's PDF export can drop slides (hidden slides, notes-only
    layouts), so the deck's slide count is NOT a reliable upper bound for
    page indexes. Returns None if pdfinfo is unavailable.
    """
    try:
        proc = subprocess.run(
            [pdfinfo_bin, str(pdf_path)], capture_output=True, text=True,
            timeout=15, start_new_session=True)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.lower().startswith("pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


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
                      page_timeout_s: int = 120,
                      parallel_pages: int = 1) -> RenderResult:
    """Render the given 0-based page indexes of a .pptx/.ppt/.pdf to PNG.

    Returns {ok, pages: {0-based index: PNG path}, skipped, failed, reason}.
    Indexes beyond the PDF's real page count (LibreOffice drops hidden/
    notes-only slides) are SKIPPED, and a single page failing to render is
    recorded in `failed` -- neither kills the other pages of the file.
    Only a genuine conversion failure (deck->pdf) fails the whole file.

    When parallel_pages > 1, pdftoppm calls run concurrently in a thread
    pool (each page is independent after the deck->PDF conversion).
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

    real_pages = _pdf_page_count(pdf_path)
    if real_pages is not None:
        # Clamp to the PDF's real page count: requesting a page that does
        # not exist makes pdftoppm exit 99 and would otherwise reject the
        # whole file. pdfinfo unavailable -> render and let failures fall
        # into `failed` individually.
        page_indexes = [i for i in page_indexes if i < real_pages]

    pages: dict[int, Path] = {}
    skipped: list[int] = []
    failed: list[int] = []
    try:
        if parallel_pages > 1 and len(page_indexes) > 1:
            # Parallel rendering: each pdftoppm call is independent.
            from concurrent.futures import ThreadPoolExecutor, as_completed
            def _render_one(idx: int) -> tuple[int, Path | None]:
                return idx, _pdftoppm(pdf_path, idx + 1, work, dpi,
                                      pdftoppm_bin=pdftoppm_bin, timeout_s=page_timeout_s)
            with ThreadPoolExecutor(max_workers=min(parallel_pages, len(page_indexes))) as pool:
                futures = {pool.submit(_render_one, idx): idx for idx in page_indexes}
                for fut in as_completed(futures):
                    try:
                        idx, png = fut.result()
                        if png is None:
                            failed.append(idx)
                        else:
                            pages[idx] = png
                    except Exception:
                        failed.append(futures[fut])
        else:
            # Sequential rendering (single page or parallel_pages=1)
            for idx in page_indexes:
                png = _pdftoppm(pdf_path, idx + 1, work, dpi,
                                pdftoppm_bin=pdftoppm_bin, timeout_s=page_timeout_s)
                if png is None:
                    failed.append(idx)
                    continue
                pages[idx] = png
        return RenderResult(True, pages=pages, skipped=skipped, failed=failed)
    finally:
        if not pages:
            shutil.rmtree(work, ignore_errors=True)
