"""Tests for ``services/pdf_extractor.py`` (PyMuPDF text extraction)."""

import fitz  # PyMuPDF — declared external dependency of the iris addon

from odoo.tests.common import tagged

from .common import IrisCase, make_pdf_bytes
from odoo.addons.iris.services.pdf_extractor import extract_text_from_pdf


@tagged("post_install", "-at_install", "iris")
class TestPdfExtractor(IrisCase):
    def test_extracts_text_from_real_pdf(self):
        data = make_pdf_bytes("Jane Doe Senior ML Engineer at Acme")
        text = extract_text_from_pdf(data, "resume.pdf")
        self.assertIn("Jane Doe Senior ML Engineer at Acme", text)
        self.assertIn("[Page 1]", text)

    def test_multi_page_pdf_labels_pages(self):
        doc = fitz.open()
        try:
            for idx in (1, 2):
                page = doc.new_page()
                page.insert_text((72, 72), f"Content of page {idx}")
            data = doc.tobytes()
        finally:
            doc.close()

        text = extract_text_from_pdf(data)
        self.assertIn("[Page 1]", text)
        self.assertIn("[Page 2]", text)
        self.assertIn("Content of page 2", text)

    def test_non_pdf_bytes_raise_value_error(self):
        with self.assertRaises(ValueError):
            extract_text_from_pdf(b"not a pdf", "resume.pdf")

    def test_empty_bytes_raise_value_error(self):
        with self.assertRaises(ValueError):
            extract_text_from_pdf(b"", "resume.pdf")

    def test_pdf_without_text_raises_value_error(self):
        # A valid PDF with one blank page has no extractable text
        # (the scanned/image-only resume case).
        doc = fitz.open()
        try:
            doc.new_page()
            data = doc.tobytes()
        finally:
            doc.close()

        with self.assertRaises(ValueError):
            extract_text_from_pdf(data, "scan.pdf")

    def test_error_message_mentions_filename(self):
        try:
            extract_text_from_pdf(b"GIF89a not a pdf", "cv-final.pdf")
        except ValueError as exc:
            self.assertIn("cv-final.pdf", str(exc))
        else:
            self.fail("expected ValueError for non-PDF input")
