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
import zipfile
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
_MAX_PDF_PAGES = 2_000
_MAX_DOCX_ENTRIES = 2_048
_MAX_DOCX_EXPANDED_BYTES = 64 * 1024 * 1024
_MAX_DOCX_COMPRESSION_RATIO = 100


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


def _extract_pdf(data: bytes, *, max_chars: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        # An empty password unlocks many "encrypted" PDFs; a real one can't be guessed.
        try:
            reader.decrypt("")
        except Exception:
            raise ValueError("PDF is password-protected") from None
    if len(reader.pages) > _MAX_PDF_PAGES:
        raise ValueError(f"PDF exceeds the {_MAX_PDF_PAGES}-page extraction limit")
    blocks: list[str] = []
    extracted = 0
    budget = max_chars if max_chars > 0 else 1_000_000
    for page in reader.pages:
        text = page.extract_text() or ""
        blocks.append(text[: max(budget - extracted, 0) + 1])
        extracted += len(text)
        if extracted > budget:
            break
    return "\n\n".join(blocks)


def _extract_docx(data: bytes, *, max_chars: int) -> str:
    from docx import Document as DocxDocument

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > _MAX_DOCX_ENTRIES:
            raise ValueError("DOCX contains too many archive entries")
        expanded_limit = min(
            _MAX_DOCX_EXPANDED_BYTES,
            max(8 * 1024 * 1024, max_chars * 32 if max_chars > 0 else 0),
        )
        expanded = sum(member.file_size for member in members)
        if expanded > expanded_limit:
            raise ValueError("DOCX expanded content exceeds the extraction limit")
        for member in members:
            if member.file_size > 0 and member.compress_size == 0:
                raise ValueError("DOCX contains an invalid compressed entry")
            if (
                member.compress_size > 0
                and member.file_size / member.compress_size > _MAX_DOCX_COMPRESSION_RATIO
            ):
                raise ValueError("DOCX archive compression ratio exceeds the safety limit")
    document = DocxDocument(io.BytesIO(data))
    blocks: list[str] = []
    extracted = 0
    budget = max_chars if max_chars > 0 else 1_000_000
    for para in document.paragraphs:
        blocks.append(para.text)
        extracted += len(para.text)
        if extracted > budget:
            return "\n".join(blocks)
    for table in document.tables:
        for row in table.rows:
            text = "\t".join(cell.text for cell in row.cells)
            blocks.append(text)
            extracted += len(text)
            if extracted > budget:
                return "\n".join(blocks)
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
            raw = _extract_pdf(data, max_chars=max_chars)
        elif kind == "docx":
            raw = _extract_docx(data, max_chars=max_chars)
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
