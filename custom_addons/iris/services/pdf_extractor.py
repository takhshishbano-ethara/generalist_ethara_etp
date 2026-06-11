"""PDF text extraction for Iris resumes.

Pure-Python helper (no Odoo imports) adapted from
``ai_services/services/document_parser.py``. Iris accepts ONLY PDF resumes,
so this module is deliberately PDF-only: it validates the ``%PDF`` magic
bytes up front and extracts page text with PyMuPDF (``fitz``).
"""

from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF"


def extract_text_from_pdf(binary_data: bytes, filename: str = "") -> str:
    """Extract plain text from raw PDF bytes.

    Args:
        binary_data: Decoded PDF file content (raw bytes, NOT base64).
        filename: Optional original filename, used only for log/error context.

    Returns:
        str: Extracted text, one ``[Page N]`` block per non-empty page.

    Raises:
        ValueError: If the input is empty, is not a PDF (missing ``%PDF``
            magic bytes), cannot be parsed, or contains no extractable text
            (e.g. a scanned/image-only PDF).
    """
    label = filename or "uploaded file"
    if not binary_data:
        raise ValueError(f"Empty file content ({label})")

    if not binary_data.lstrip()[:4] == _PDF_MAGIC:
        raise ValueError(
            f"{label} is not a valid PDF (missing %PDF header). "
            "Only PDF resumes are supported."
        )

    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ValueError("PyMuPDF is not installed. Run: pip install PyMuPDF")

    try:
        doc = fitz.open(stream=binary_data, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Could not parse {label} as PDF: {exc}") from exc

    try:
        pages = []
        for page_num, page in enumerate(doc, 1):
            text = page.get_text("text")
            if text.strip():
                pages.append(f"[Page {page_num}]\n{text.strip()}")
    finally:
        doc.close()

    if not pages:
        raise ValueError(
            f"{label} contains no extractable text (might be scanned/image-based)"
        )

    extracted = "\n\n".join(pages)
    _logger.info("Extracted %d chars from %s (%d pages)", len(extracted), label, len(pages))
    return extracted
