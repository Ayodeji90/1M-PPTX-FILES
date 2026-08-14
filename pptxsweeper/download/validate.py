"""Downloaded-payload validation.

Two layers, both mandatory before a file may progress:
1. Magic bytes:  PK\\x03\\x04 (OOXML/zip) or %PDF.
   Legacy .ppt uses the OLE2 magic D0 CF 11 E0 A1 B1 1A E1.
2. Full open-validation: unzip + presentation.xml present for pptx,
   fitz.open for pdf, olefile-light structural check for ppt.
"""
from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pptxsweeper.download.validate")

MAGIC_ZIP = b"PK\x03\x04"
MAGIC_PDF = b"%PDF"
MAGIC_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@dataclass
class ValidationResult:
    ok: bool
    format: str = ""       # pptx | pdf | ppt
    reason: str = ""
    slide_count: int = 0   # best-effort; classify recomputes authoritatively


def sniff_format(path: str | Path) -> str | None:
    """Return 'zip' | 'pdf' | 'ole2' | None from magic bytes."""
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(MAGIC_ZIP):
        return "zip"
    if head.startswith(MAGIC_PDF):
        return "pdf"
    if head.startswith(MAGIC_OLE2):
        return "ole2"
    return None


def _validate_pptx(path: Path) -> ValidationResult:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "ppt/presentation.xml" not in names:
                if "word/document.xml" in names or "xl/workbook.xml" in names:
                    return ValidationResult(False, reason="OOXML but not a presentation (docx/xlsx)")
                return ValidationResult(False, reason="zip without ppt/presentation.xml")
            bad = zf.testzip()
            if bad is not None:
                return ValidationResult(False, reason=f"corrupt zip member: {bad}")
            # parse presentation.xml to prove it's well-formed XML
            from ..quality.ooxml import _parse_xml, slide_paths_in_order, PptxParseError
            try:
                _parse_xml(zf.read("ppt/presentation.xml"))
                slides = slide_paths_in_order(zf)
            except PptxParseError as exc:
                return ValidationResult(False, reason=f"presentation.xml unparseable: {exc}")
            return ValidationResult(True, format="pptx", slide_count=len(slides))
    except zipfile.BadZipFile as exc:
        return ValidationResult(False, reason=f"bad zip: {exc}")
    except OSError as exc:
        return ValidationResult(False, reason=f"io error: {exc}")
    except Exception as exc:
        # Corrupt/truncated zips surface as zlib.error (e.g. "invalid
        # stored block lengths"), struct.error, EOFError, etc. during
        # testzip()/read(). These must REJECT the file, never propagate:
        # a single corrupt deck previously crashed the classify worker
        # (BrokenProcessPool) and took the whole deliver chain down.
        return ValidationResult(False, reason=f"corrupt zip: {exc}")


def _validate_pdf(path: Path) -> ValidationResult:
    try:
        import fitz
    except ImportError:
        return ValidationResult(False, reason="PyMuPDF not installed")
    try:
        with fitz.open(str(path)) as doc:
            if doc.needs_pass:
                return ValidationResult(False, reason="password-protected PDF")
            count = doc.page_count
            if count == 0:
                return ValidationResult(False, reason="zero pages")
            # touch first + last page to catch truncated bodies
            doc.load_page(0)
            doc.load_page(count - 1)
            return ValidationResult(True, format="pdf", slide_count=count)
    except Exception as exc:
        return ValidationResult(False, reason=f"pdf open failed: {exc}")


def _validate_ppt(path: Path) -> ValidationResult:
    # Structural sanity for OLE2 compound files: header magic (already
    # checked) + sane sector size + minimum size. Full semantic
    # validation happens implicitly at LibreOffice conversion time --
    # a broken .ppt fails conversion and is rejected there.
    size = path.stat().st_size
    if size < 4096:
        return ValidationResult(False, reason=f"ole2 file too small ({size}B)")
    with open(path, "rb") as fh:
        header = fh.read(512)
    if len(header) < 512:
        return ValidationResult(False, reason="ole2 header truncated")
    sector_shift = int.from_bytes(header[30:32], "little")
    if sector_shift not in (9, 12):
        return ValidationResult(False, reason=f"ole2 bad sector shift {sector_shift}")
    return ValidationResult(True, format="ppt")


def validate_payload(path: str | Path, claimed_ext: str | None = None) -> ValidationResult:
    """Validate a downloaded body. Format is decided by MAGIC BYTES, not
    by the URL's extension (servers lie); claimed_ext only disambiguates
    zip -> pptx expectations for the error message."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return ValidationResult(False, reason="empty or missing payload")
    magic = sniff_format(path)
    if magic == "zip":
        return _validate_pptx(path)
    if magic == "pdf":
        return _validate_pdf(path)
    if magic == "ole2":
        return _validate_ppt(path)
    return ValidationResult(False, reason=f"unknown magic bytes (claimed {claimed_ext or '?'})")
