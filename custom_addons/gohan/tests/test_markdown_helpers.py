from io import BytesIO

from PIL import Image

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..models.gohan_job import (
    _markdown_to_html,
    _resize_image_for_bedrock,
    _svg_to_png,
)


@tagged("post_install", "-at_install", "gohan")
class TestMarkdownToHtml(TransactionCase):

    def test_empty_returns_empty(self):
        self.assertEqual(_markdown_to_html(""), "")
        self.assertEqual(_markdown_to_html(None), "")  # type: ignore[arg-type]

    def test_headings_h1_h4(self):
        out = _markdown_to_html("# A\n## B\n### C\n#### D\n")
        self.assertIn("<h1>A</h1>", out)
        self.assertIn("<h2>B</h2>", out)
        self.assertIn("<h3>C</h3>", out)
        self.assertIn("<h4>D</h4>", out)

    def test_unordered_list(self):
        out = _markdown_to_html("- one\n- two\n")
        self.assertIn("<ul>", out)
        self.assertIn("<li>one</li>", out)
        self.assertIn("<li>two</li>", out)
        self.assertIn("</ul>", out)

    def test_star_bullet_also_recognised(self):
        out = _markdown_to_html("* one\n* two\n")
        self.assertIn("<ul>", out)
        self.assertIn("<li>one</li>", out)

    def test_inline_bold_italic_code(self):
        out = _markdown_to_html("Text with **bold**, *em*, and `code`.")
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<em>em</em>", out)
        self.assertIn("<code>code</code>", out)

    def test_table_with_separator_row(self):
        md = "| col | col |\n| --- | --- |\n| a | b |\n"
        out = _markdown_to_html(md)
        self.assertIn("<table", out)
        self.assertIn("<td>a</td>", out)
        self.assertIn("<td>b</td>", out)
        self.assertNotIn("<td>---</td>", out)

    def test_list_closes_when_heading_follows(self):
        out = _markdown_to_html("- one\n- two\n# Heading\n")
        self.assertLess(out.index("</ul>"), out.index("<h1>"))

    def test_paragraph_wrapping(self):
        out = _markdown_to_html("First paragraph.\n\nSecond paragraph.\n")
        self.assertIn("<p>First paragraph.</p>", out)
        self.assertIn("<p>Second paragraph.</p>", out)


def _make_png(width, height, mode="RGB", color=(255, 0, 0)):
    buf = BytesIO()
    img = Image.new(mode, (width, height), color)
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg(width, height, color=(0, 255, 0)):
    buf = BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


@tagged("post_install", "-at_install", "gohan")
class TestResizeImage(TransactionCase):

    def test_under_limit_passes_through(self):
        original = _make_png(100, 100)
        result = _resize_image_for_bedrock(original, "png")
        self.assertEqual(result, original)

    def test_over_limit_downscaled(self):
        original = _make_png(9000, 4500)
        result = _resize_image_for_bedrock(original, "png")
        self.assertNotEqual(result, original)
        img = Image.open(BytesIO(result))
        self.assertLessEqual(max(img.size), 7800)
        self.assertEqual(img.size[0] / img.size[1], 2.0)

    def test_jpeg_path(self):
        original = _make_jpeg(9000, 4500)
        result = _resize_image_for_bedrock(original, "jpeg")
        img = Image.open(BytesIO(result))
        self.assertLessEqual(max(img.size), 7800)
        self.assertEqual(img.format, "JPEG")

    def test_rgba_converted_for_jpeg(self):
        original = _make_png(9000, 1000, mode="RGBA", color=(255, 0, 0, 128))
        result = _resize_image_for_bedrock(original, "jpeg")
        img = Image.open(BytesIO(result))
        self.assertEqual(img.format, "JPEG")
        self.assertEqual(img.mode, "RGB")

    def test_decode_failure_returns_original(self):
        garbage = b"not an image"
        result = _resize_image_for_bedrock(garbage, "png")
        self.assertEqual(result, garbage)


@tagged("post_install", "-at_install", "gohan")
class TestSvgToPng(TransactionCase):

    def test_empty_returns_none(self):
        self.assertIsNone(_svg_to_png(b""))
        self.assertIsNone(_svg_to_png(None))  # type: ignore[arg-type]

    def test_invalid_svg_returns_none(self):
        # Garbage input never raises -- returns None whether or not the
        # optional cairosvg dependency is installed.
        self.assertIsNone(_svg_to_png(b"this is not svg markup"))
