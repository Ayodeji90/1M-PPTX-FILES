"""Magic-byte + open-validation tests."""
from __future__ import annotations

from pptxsweeper.download.validate import validate_payload, sniff_format


def test_pptx_magic_and_open(decks):
    result = validate_payload(decks["chart_heavy"])
    assert result.ok and result.format == "pptx"
    assert result.slide_count == 10


def test_pdf_magic_and_open(tmp_path):
    import fitz
    doc = fitz.open()
    for i in range(6):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i}")
    p = tmp_path / "ok.pdf"
    doc.save(p)
    result = validate_payload(p)
    assert result.ok and result.format == "pdf" and result.slide_count == 6


def test_html_error_page_rejected(tmp_path):
    p = tmp_path / "fake.pptx"
    p.write_bytes(b"<html><body>404 Not Found</body></html>")
    assert sniff_format(p) is None
    result = validate_payload(p)
    assert not result.ok and "magic" in result.reason


def test_corrupt_zip_rejected(tmp_path):
    p = tmp_path / "corrupt.pptx"
    p.write_bytes(b"PK\x03\x04" + b"garbage" * 100)
    result = validate_payload(p)
    assert not result.ok


def test_docx_masquerading_as_pptx_rejected(tmp_path):
    import zipfile
    p = tmp_path / "really_a_docx.pptx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")
    result = validate_payload(p)
    assert not result.ok and "not a presentation" in result.reason


def test_ole2_ppt_sniffed(tmp_path):
    p = tmp_path / "legacy.ppt"
    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    header[30:32] = (9).to_bytes(2, "little")
    p.write_bytes(bytes(header) + b"\x00" * 8192)
    result = validate_payload(p)
    assert result.ok and result.format == "ppt"


def test_empty_file_rejected(tmp_path):
    p = tmp_path / "empty.pptx"
    p.write_bytes(b"")
    assert not validate_payload(p).ok
