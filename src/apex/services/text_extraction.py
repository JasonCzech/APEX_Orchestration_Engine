"""Extract plain text from uploaded context documents (PDF / DOCX / Markdown / plain text).

Story Analysis (and any context-consuming phase) reads uploaded files as evidence, so the
bytes a user uploads must become text the model can actually read. This is a pure, local,
synchronous extractor — no external service — dispatched by file extension first (more
reliable than the browser-supplied content type), then by MIME as a fallback.

Failures never raise to the caller: an unsupported type reports ``unsupported`` and a
corrupt-but-supported file reports ``failed`` so the upload still succeeds and the file
stays attached as a titled reference. Callers run :func:`extract_text` in a worker thread
(``asyncio.to_thread``) because ``pypdf`` and ``python-docx`` are CPU-bound and blocking.
"""

import io
from dataclasses import dataclass
from posixpath import splitext

# Parse-status values surfaced through the API/UI.
PARSE_PARSED = "parsed"
PARSE_FAILED = "failed"
PARSE_UNSUPPORTED = "unsupported"

# Extensions we know how to turn into text.
_PDF_EXTS = {".pdf"}
_DOCX_EXTS = {".docx"}
_TEXT_EXTS = {".md", ".markdown", ".txt", ".text"}

# MIME fallbacks for when the filename has no useful extension.
_PDF_MIMES = {"application/pdf"}
_DOCX_MIMES = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
_TEXT_MIMES = {"text/markdown", "text/x-markdown", "text/plain"}


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of a single extraction.

    ``text`` is the normalized (and storage-capped) content; ``char_count`` is the full
    extracted length *before* any cap, so callers can report a document's true size.
    """

    text: str
    status: str
    char_count: int
    error: str | None = None


def _kind(filename: str, content_type: str | None) -> str | None:
    """Resolve an extractor kind ("pdf" | "docx" | "text") from extension, then MIME."""
    ext = splitext((filename or "").lower())[1]
    if ext in _PDF_EXTS:
        return "pdf"
    if ext in _DOCX_EXTS:
        return "docx"
    if ext in _TEXT_EXTS:
        return "text"
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in _PDF_MIMES:
        return "pdf"
    if mime in _DOCX_MIMES:
        return "docx"
    if mime in _TEXT_MIMES or (mime.startswith("text/") and mime != "text/"):
        return "text"
    return None


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        # An empty password unlocks many "encrypted" PDFs; a real one can't be guessed.
        try:
            reader.decrypt("")
        except Exception:
            raise ValueError("PDF is password-protected") from None
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(data: bytes) -> str:
    from docx import Document as DocxDocument

    document = DocxDocument(io.BytesIO(data))
    blocks: list[str] = [para.text for para in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            blocks.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(blocks)


def _extract_plaintext(data: bytes) -> str:
    # Markdown is passed through verbatim — the model reads it directly. Decode as UTF-8,
    # falling back to a lossy decode only when that fails outright.
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _normalize(text: str) -> str:
    """Normalize newlines, strip trailing per-line whitespace, and trim outer blank lines."""
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in unified.split("\n")).strip()


def extract_text(
    data: bytes,
    *,
    filename: str,
    content_type: str | None,
    max_chars: int,
) -> ExtractionResult:
    """Best-effort text extraction. Reports status instead of raising on content problems."""
    kind = _kind(filename, content_type)
    if kind is None:
        return ExtractionResult(text="", status=PARSE_UNSUPPORTED, char_count=0)
    try:
        if kind == "pdf":
            raw = _extract_pdf(data)
        elif kind == "docx":
            raw = _extract_docx(data)
        else:
            raw = _extract_plaintext(data)
    except Exception as exc:
        return ExtractionResult(text="", status=PARSE_FAILED, char_count=0, error=str(exc)[:500])

    text = _normalize(raw)
    full_len = len(text)
    if max_chars > 0 and full_len > max_chars:
        text = text[:max_chars].rstrip() + f"\n\n…[truncated {full_len - max_chars} characters]"
    return ExtractionResult(text=text, status=PARSE_PARSED, char_count=full_len)


def derive_summary(text: str, *, max_chars: int) -> str | None:
    """Heuristic one-line summary: the first non-empty paragraph, whitespace-collapsed and capped.

    Used when the uploader supplies no summary, so the evidence list still has a readable
    label. Deliberately not an LLM call — no cost or latency added to the upload path.
    """
    for block in text.split("\n\n"):
        candidate = " ".join(block.split())
        if candidate:
            if max_chars > 0 and len(candidate) > max_chars:
                return candidate[: max_chars - 1].rstrip() + "…"
            return candidate
    return None
