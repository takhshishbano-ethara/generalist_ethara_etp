"""Take-home / FSD submission text extraction (P2-8).

Pure-Python helper (no Odoo imports). Unlike resumes (PDF-only, see
``pdf_extractor``), assessment submissions arrive in several formats:

* **PDF** — detected by the ``%PDF`` magic bytes (or ``.pdf`` extension)
  and delegated to :mod:`.pdf_extractor`.
* **DOCX** — ZIP magic (``PK\\x03\\x04``) + ``.docx`` extension; extracted
  with ``python-docx``. This is a SOFT dependency (same pattern as
  ``ai_services/services/document_parser.py``): the import is lazy and a
  missing library surfaces as a clean ``ValueError`` — it never blocks the
  iris install and is deliberately NOT in the manifest's
  ``external_dependencies``.
* **Markdown / plain text** (``.md`` / ``.txt``) — UTF-8 decoded with
  ``errors="replace"`` (a stray byte never kills a submission upload).

Anything else raises ``ValueError`` listing the supported types; the model
layer converts that into a user-facing error.
"""

from __future__ import annotations

import io
import logging

from . import pdf_extractor

_logger = logging.getLogger(__name__)

#: Supported submission file extensions (advertised in error messages).
SUPPORTED_EXTENSIONS = ("pdf", "docx", "md", "txt")

_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"


def _file_extension(filename: str | None) -> str:
    """Lowercased extension of ``filename`` ('' when none)."""
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def extract_submission_text(binary_data: bytes, filename: str = "") -> str:
    """Extract plain text from a submission file.

    Args:
        binary_data: Decoded file content (raw bytes, NOT base64).
        filename: Original filename — drives extension-based dispatch and
            error context.

    Returns:
        str: The extracted text.

    Raises:
        ValueError: Empty content, unsupported type, corrupt file, missing
            soft dependency (``python-docx``), or no extractable text.
    """
    label = filename or "uploaded file"
    if not binary_data:
        raise ValueError(f"Empty file content ({label})")

    ext = _file_extension(filename)

    # PDF: magic bytes are authoritative; the extension is a fallback so a
    # mis-named PDF still gets the precise pdf_extractor error message.
    if binary_data.lstrip()[:4] == _PDF_MAGIC or ext == "pdf":
        return pdf_extractor.extract_text_from_pdf(binary_data, filename=filename)

    if ext == "docx":
        if binary_data[:4] != _ZIP_MAGIC:
            raise ValueError(
                f"{label} is not a valid .docx file (missing ZIP header)."
            )
        return _extract_docx(binary_data)

    if ext in ("md", "txt"):
        return binary_data.decode("utf-8", errors="replace")

    raise ValueError(
        f"Unsupported submission type for {label}. "
        f"Supported types: {', '.join('.' + e for e in SUPPORTED_EXTENSIONS)}."
    )


def _extract_docx(file_bytes: bytes) -> str:
    """Extract text from DOCX using python-docx (lazy soft dependency)."""
    try:
        from docx import Document
    except ImportError:
        raise ValueError(
            "python-docx is not installed. Run: pip install python-docx"
        )

    doc = Document(io.BytesIO(file_bytes))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]

    if not paragraphs:
        raise ValueError("DOCX contains no text content")
    return "\n".join(paragraphs)
