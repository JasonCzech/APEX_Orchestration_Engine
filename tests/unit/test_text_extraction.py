"""Unit tests for the context-document text extractor.

Covers the dispatch matrix (extension first, MIME fallback), each supported
format (PDF / DOCX / Markdown / plain text), the soft-fail contract for
unsupported and corrupt inputs, storage-cap truncation, and the heuristic
summary derivation.
"""

import io

import pytest
from docx import Document as DocxDocument

from apex.services.text_extraction import (
    PARSE_FAILED,
    PARSE_PARSED,
    PARSE_UNSUPPORTED,
    _kind,
    derive_summary,
    extract_text,
)


def _make_pdf(text: str) -> bytes:
    """Build a minimal single-page PDF with one Tj text-showing operator.

    Offsets in the xref table are computed against the assembled bytes so pypdf
    parses it without falling back to object scanning.
    """
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n"
    out += f"0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += b"trailer\n"
    out += f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += b"startxref\n"
    out += f"{xref_pos}\n".encode()
    out += b"%%EOF"
    return bytes(out)


def _make_docx(paragraphs: list[str], table_rows: list[list[str]] | None = None) -> bytes:
    document = DocxDocument()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    if table_rows:
        table = document.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for r, row in enumerate(table_rows):
            for c, value in enumerate(row):
                table.cell(r, c).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("spec.pdf", None, "pdf"),
        ("spec.PDF", None, "pdf"),
        ("notes.docx", None, "docx"),
        ("readme.md", None, "text"),
        ("readme.markdown", None, "text"),
        ("plain.txt", None, "text"),
        # Extension wins over a mismatched content type.
        ("doc.pdf", "text/plain", "pdf"),
        # No useful extension -> fall back to MIME.
        ("blob", "application/pdf", "pdf"),
        (
            "blob",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        ),
        ("blob", "text/markdown", "text"),
        ("blob", "text/csv", "text"),  # any text/* is treated as plain text
        ("archive.zip", "application/zip", None),
        ("image.png", "image/png", None),
    ],
)
def test_kind_dispatch(filename: str, content_type: str | None, expected: str | None) -> None:
    assert _kind(filename, content_type) == expected


def test_extract_plaintext_normalizes_newlines() -> None:
    data = b"first line\r\nsecond line  \r\n\r\n"
    result = extract_text(data, filename="a.txt", content_type=None, max_chars=10_000)
    assert result.status == PARSE_PARSED
    assert result.text == "first line\nsecond line"
    assert result.char_count == len("first line\nsecond line")


def test_extract_markdown_passthrough() -> None:
    data = b"# Title\n\nSome **bold** evidence.\n"
    result = extract_text(data, filename="story.md", content_type=None, max_chars=10_000)
    assert result.status == PARSE_PARSED
    assert "# Title" in result.text
    assert "**bold**" in result.text


def test_extract_plaintext_lossy_fallback_on_invalid_utf8() -> None:
    data = b"valid \xff\xfe bytes"
    result = extract_text(data, filename="weird.txt", content_type=None, max_chars=10_000)
    assert result.status == PARSE_PARSED
    assert "valid" in result.text
    assert "bytes" in result.text


def test_extract_docx_paragraphs_and_tables() -> None:
    data = _make_docx(
        ["Heading paragraph", "Second paragraph"],
        table_rows=[["r1c1", "r1c2"], ["r2c1", "r2c2"]],
    )
    result = extract_text(data, filename="notes.docx", content_type=None, max_chars=10_000)
    assert result.status == PARSE_PARSED
    assert "Heading paragraph" in result.text
    assert "Second paragraph" in result.text
    assert "r1c1\tr1c2" in result.text
    assert "r2c1\tr2c2" in result.text


def test_extract_pdf_returns_text() -> None:
    data = _make_pdf("Hello PDF context")
    result = extract_text(data, filename="spec.pdf", content_type=None, max_chars=10_000)
    assert result.status == PARSE_PARSED
    assert "Hello" in result.text
    assert "context" in result.text


def test_unsupported_type_reports_unsupported() -> None:
    result = extract_text(b"\x00\x01\x02", filename="archive.zip", content_type=None, max_chars=100)
    assert result.status == PARSE_UNSUPPORTED
    assert result.text == ""
    assert result.char_count == 0
    assert result.error is None


def test_corrupt_pdf_soft_fails() -> None:
    # A .pdf extension routes to the PDF extractor, but the bytes are not a PDF.
    result = extract_text(
        b"this is not a pdf", filename="broken.pdf", content_type=None, max_chars=100
    )
    assert result.status == PARSE_FAILED
    assert result.text == ""
    assert result.error is not None


def test_truncation_applies_storage_cap_and_reports_full_length() -> None:
    body = "abcdefghij" * 10  # 100 chars
    result = extract_text(body.encode(), filename="big.txt", content_type=None, max_chars=25)
    assert result.status == PARSE_PARSED
    assert result.char_count == 100
    assert "…[truncated 75 characters]" in result.text
    # The retained slice never exceeds the cap (before the marker).
    assert result.text.split("\n\n…[truncated")[0] == body[:25].rstrip()


def test_no_truncation_when_under_cap() -> None:
    result = extract_text(b"short", filename="s.txt", content_type=None, max_chars=1_000)
    assert result.text == "short"
    assert "truncated" not in result.text


def test_max_chars_zero_disables_truncation() -> None:
    body = ("x" * 500).encode()
    result = extract_text(body, filename="s.txt", content_type=None, max_chars=0)
    assert result.char_count == 500
    assert "truncated" not in result.text


def test_derive_summary_uses_first_nonempty_paragraph() -> None:
    text = "\n\n  \n\nFirst real   paragraph here.\n\nSecond paragraph."
    assert derive_summary(text, max_chars=280) == "First real paragraph here."


def test_derive_summary_caps_with_ellipsis() -> None:
    summary = derive_summary("word " * 100, max_chars=20)
    assert summary is not None
    assert len(summary) == 20
    assert summary.endswith("…")


def test_derive_summary_none_for_blank() -> None:
    assert derive_summary("\n\n   \n\n", max_chars=280) is None
