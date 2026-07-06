"""Content extraction: uploaded bytes → plain text + light structure.

Structure survives as markdown-ish markers (headings as `## `, PDF page
breaks as form feeds) so the Increment-3 chunker can split at real
boundaries instead of mid-sentence. Failures raise ExtractionError with
a user-safe message — one document failing never fails the batch.
"""
import io
import os

from pypdf import PdfReader
from docx import Document as DocxDocument

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".markdown"}

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".docx": ("application/vnd.openxmlformats-officedocument"
              ".wordprocessingml.document"),
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}


class ExtractionError(Exception):
    """Message is shown to the user — keep it friendly and specific."""


def _extract_pdf(data: bytes):
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception:
        raise ExtractionError("Couldn't read this PDF — it may be corrupted "
                              "or password-protected.")
    text = "\f".join(pages).strip()  # form feed = page boundary for chunking
    if not text:
        raise ExtractionError("No extractable text — this PDF looks scanned. "
                              "OCR isn't supported yet.")
    return text, {"pages": len(pages)}


def _extract_docx(data: bytes):
    try:
        doc = DocxDocument(io.BytesIO(data))
    except Exception:
        raise ExtractionError("Couldn't read this DOCX file — it may be "
                              "corrupted or an older .doc format.")
    lines = []
    for para in doc.paragraphs:
        stripped = para.text.strip()
        if not stripped:
            continue
        style = (para.style.name or "") if para.style else ""
        if style.startswith("Heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "2"
            lines.append("#" * min(int(level), 6) + " " + stripped)
        else:
            lines.append(stripped)
    text = "\n\n".join(lines).strip()
    if not text:
        raise ExtractionError("This DOCX file contains no extractable text.")
    return text, {"paragraphs": len(lines)}


def _extract_text(data: bytes):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    text = text.strip()
    if not text:
        raise ExtractionError("This file is empty.")
    return text, {}


def extract_document(filename: str, data: bytes):
    """→ (text, meta). Raises ExtractionError with a user-safe reason."""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return _extract_pdf(data)
    if ext == ".docx":
        return _extract_docx(data)
    if ext in (".txt", ".md", ".markdown"):
        return _extract_text(data)
    raise ExtractionError(f"Unsupported file type: {ext or 'no extension'}")
