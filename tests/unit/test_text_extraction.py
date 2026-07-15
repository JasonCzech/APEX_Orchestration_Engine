"""Unit tests for the context-document text extractor.

Covers the dispatch matrix (extension first, MIME fallback), each supported
format (PDF / DOCX / Markdown / plain text), the soft-fail contract for
unsupported and corrupt inputs, storage-cap truncation, and the heuristic
summary derivation.
"""

import io
import subprocess
import tracemalloc
import zlib
from types import SimpleNamespace

import docx
import pytest
from docx import Document as DocxDocument

import apex.services.text_extraction as extraction_module
from apex.services.text_extraction import (
    PARSE_FAILED,
    PARSE_PARSED,
    PARSE_UNSUPPORTED,
    _extract_docx_in_worker,
    _kind,
    derive_summary,
    extract_text,
)


def _make_pdf_stream(content: bytes, *, compressed: bool = False) -> bytes:
    """Build a minimal single-page PDF around one content stream.

    Offsets in the xref table are computed against the assembled bytes so pypdf
    parses it without falling back to object scanning.
    """
    if compressed:
        content = zlib.compress(content)
        filter_entry = b" /Filter /FlateDecode"
    else:
        filter_entry = b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(content)).encode()
        + filter_entry
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
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


def _make_pdf(text: str) -> bytes:
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
    return _make_pdf_stream(content)


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


def test_plaintext_normalization_is_chunk_bounded_for_newline_bomb() -> None:
    # The old whole-document split created one Python list entry per newline
    # (hundreds of MiB at the 25 MiB upload limit). Input is allocated before
    # tracing so the assertion measures extraction overhead only.
    data = b"\n" * (8 * 1024 * 1024)
    tracemalloc.start()
    try:
        result = extract_text(data, filename="newlines.txt", content_type=None, max_chars=200_000)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.status == PARSE_PARSED
    assert result.text == ""
    assert result.char_count == 0
    assert peak < 2 * 1024 * 1024


def test_plaintext_normalizes_crlf_split_across_decode_chunks() -> None:
    prefix = b"a" * (extraction_module._DECODE_CHUNK_BYTES - 1)
    result = extract_text(
        prefix + b"\r\nnext  \rfinal",
        filename="boundary.txt",
        content_type=None,
        max_chars=100_000,
    )
    expected = prefix.decode() + "\nnext\nfinal"
    assert result.text == expected
    assert result.char_count == len(expected)


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


def test_docx_feeds_every_large_block_without_retaining_block_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Zip validation still receives a real DOCX, while the document object makes
    # block sizes deterministic and proves later blocks are visited after the
    # retained prefix is full.
    valid_docx = _make_docx(["stub"])
    paragraphs = [SimpleNamespace(text="a" * 200_000), SimpleNamespace(text="tail")]
    fake_document = SimpleNamespace(paragraphs=paragraphs, tables=[])
    monkeypatch.setattr(docx, "Document", lambda _stream: fake_document)

    extracted = _extract_docx_in_worker(valid_docx, max_chars=64)

    assert extracted.text == "a" * 64
    assert extracted.char_count == 200_005


def test_extract_pdf_returns_text() -> None:
    data = _make_pdf("Hello PDF context")
    result = extract_text(data, filename="spec.pdf", content_type=None, max_chars=10_000)
    assert result.status == PARSE_PARSED
    assert "Hello" in result.text
    assert "context" in result.text


def test_pdf_rejects_small_compressed_input_with_oversized_decoded_stream() -> None:
    decoded = b" " * (extraction_module._MAX_PDF_DECODED_STREAM_BYTES + 1)
    data = _make_pdf_stream(decoded, compressed=True)
    assert len(data) < 100_000

    result = extract_text(data, filename="bomb.pdf", content_type=None, max_chars=10_000)

    assert result.status == PARSE_FAILED
    assert result.text == ""
    assert result.error is not None
    assert "limit" in result.error.lower()


@pytest.mark.parametrize(
    ("filename", "data", "worker_module"),
    [
        ("isolated.pdf", b"%PDF", "apex.services.pdf_extraction_worker"),
        ("isolated.docx", b"PK", "apex.services.docx_extraction_worker"),
    ],
)
def test_complex_format_workers_do_not_inherit_server_secrets(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    data: bytes,
    worker_module: str,
) -> None:
    sensitive_names = {
        "APEX_AUTH__DEV_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URI",
        "JIRA_TOKEN",
        "REDIS_URI",
    }
    for name in sensitive_names:
        monkeypatch.setenv(name, "sentinel-secret-must-not-reach-worker")

    captured_environment: dict[str, str] | None = None

    class SuccessfulProcess:
        returncode = 0

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input, timeout
            return b"S" + (0).to_bytes(8, "big"), b""

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        env: dict[str, str],
    ) -> SuccessfulProcess:
        nonlocal captured_environment
        del stdin, stdout, stderr
        assert command[1:4] == ["-I", "-m", worker_module]
        captured_environment = dict(env)
        return SuccessfulProcess()

    monkeypatch.setattr(extraction_module.subprocess, "Popen", fake_popen)

    result = extract_text(data, filename=filename, content_type=None, max_chars=100)

    assert result.status == PARSE_PARSED
    assert captured_environment == extraction_module._EXTRACTION_WORKER_ENV
    assert captured_environment is not None
    assert sensitive_names.isdisjoint(captured_environment)


def test_pdf_timeout_kills_and_reaps_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, float | None]] = []

    class HungProcess:
        returncode = -9

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input
            calls.append(("communicate", timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd="pdf-worker", timeout=timeout or 0)
            return b"", b""

        def kill(self) -> None:
            calls.append(("kill", None))

    monkeypatch.setattr(
        extraction_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: HungProcess(),
    )

    result = extract_text(b"%PDF", filename="hung.pdf", content_type=None, max_chars=100)

    assert result.status == PARSE_FAILED
    assert result.error is not None and "second limit" in result.error
    assert calls == [
        ("communicate", extraction_module._PDF_WORKER_WALL_SECONDS),
        ("kill", None),
        ("communicate", extraction_module._PDF_WORKER_REAP_SECONDS),
    ]


def test_docx_timeout_kills_and_reaps_isolated_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float | None]] = []

    class HungProcess:
        returncode = -9

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input
            calls.append(("communicate", timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd="docx-worker", timeout=timeout or 0)
            return b"", b""

        def kill(self) -> None:
            calls.append(("kill", None))

    monkeypatch.setattr(
        extraction_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: HungProcess(),
    )

    result = extract_text(
        _make_docx(["untrusted"]),
        filename="hung.docx",
        content_type=None,
        max_chars=100,
    )

    assert result.status == PARSE_FAILED
    assert result.error is not None and "second limit" in result.error
    assert calls == [
        ("communicate", extraction_module._DOCX_WORKER_WALL_SECONDS),
        ("kill", None),
        ("communicate", extraction_module._DOCX_WORKER_REAP_SECONDS),
    ]


def test_pdf_worker_response_rejects_output_beyond_retention_budget() -> None:
    payload = b"S" + (101).to_bytes(8, "big") + (b"x" * 101)
    with pytest.raises(ValueError, match="output limit"):
        extraction_module._decode_pdf_worker_response(payload, max_chars=25)


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


def test_extraction_failure_diagnostic_redacts_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "parser-secret-canary-9d4a"

    def fail(*_args: object, **_kwargs: object) -> object:
        raise ValueError(f"password={canary}")

    monkeypatch.setattr(extraction_module, "_extract_plaintext", fail)
    result = extract_text(b"body", filename="broken.txt", content_type=None, max_chars=100)

    assert result.status == PARSE_FAILED
    assert result.error is not None
    assert canary not in result.error
    assert "[REDACTED]" in result.error


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
