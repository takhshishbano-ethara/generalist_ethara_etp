"""Curatable extraction assets for a Gohan job.

Every screenshot, section snapshot, SVG icon, image, and font produced by the
extraction Lambda is materialized into one ``gohan.job.asset`` row. The tasker
reviews these in the Extraction Review tab while the job waits in the
``extracted`` state, can remove anything unwanted, and can upload extra SVG
icons. PRD generation then feeds only the rows with ``include_in_prd`` ticked:
screenshots/snapshots as Bedrock images, SVG icons as inline markup.
"""

import base64
import logging
import re

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Asset types that are sent to Bedrock as image blocks during PRD generation.
IMAGE_ASSET_TYPES = ("screenshot", "section_snapshot", "image")

# Strip active content from operator-uploaded SVGs before the markup is
# stored and rendered inline in the preview (the Lambda already sanitizes
# extracted icons). Cheap defence against a malicious uploaded .svg file.
_SVG_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_SVG_EVENT_RE = re.compile(r"""\son\w+\s*=\s*("[^"]*"|'[^']*')""", re.IGNORECASE)


def _sanitize_svg_markup(markup):
    """Remove <script> blocks and inline on* event handlers from SVG markup."""
    if not markup:
        return markup
    cleaned = _SVG_SCRIPT_RE.sub("", markup)
    cleaned = _SVG_EVENT_RE.sub("", cleaned)
    return cleaned


class GohanJobAsset(models.Model):
    _name = "gohan.job.asset"
    _description = "Gohan Extraction Asset"
    _order = "sequence, id"

    job_id = fields.Many2one(
        "gohan.job",
        string="Job",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(default=20)
    name = fields.Char(string="Name", required=True, default="Asset")
    asset_type = fields.Selection(
        [
            ("screenshot", "Screenshot"),
            ("section_snapshot", "Section Snapshot"),
            ("svg", "SVG Icon"),
            ("image", "Image"),
            ("font", "Font"),
            ("other", "Other"),
        ],
        string="Type",
        required=True,
        default="other",
    )
    source = fields.Selection(
        [
            ("extracted", "Extracted"),
            ("uploaded", "Uploaded by Operator"),
        ],
        string="Source",
        required=True,
        default="uploaded",
    )
    include_in_prd = fields.Boolean(
        string="Use in PRD",
        default=True,
        help=(
            "When ticked, this asset is fed to PRD generation: screenshots "
            "and section snapshots as images, SVG icons as inline markup. "
            "Untick (or delete the row) to keep it out of the PRD."
        ),
    )
    s3_key = fields.Char(
        string="S3 Key",
        help="S3 object key for an extracted asset.",
    )
    file_data = fields.Binary(string="File", attachment=True)
    file_name = fields.Char(string="File Name")
    content_text = fields.Text(
        string="SVG Markup",
        help=(
            "Inline SVG markup fed to PRD generation. Auto-filled for "
            "extracted icons and for uploaded .svg files."
        ),
    )
    mime_type = fields.Char(string="MIME Type")
    is_copyright_safe = fields.Boolean(
        string="Copyright-Safe",
        help=(
            "Set by the Lambda for generic, reusable UI icons. The brand "
            "logo and original illustrations are excluded from extraction."
        ),
    )
    is_logo = fields.Boolean(string="Brand Logo")
    notes = fields.Char(string="Notes")
    preview_html = fields.Html(
        string="Preview",
        compute="_compute_preview_html",
        sanitize=False,
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _s3_base_url(self):
        """Public base URL for S3-stored assets (CDN first, then bucket)."""
        ICP = self.env["ir.config_parameter"].sudo()
        cdn = (ICP.get_param("gohan.s3_cdn_url") or "").rstrip("/")
        if cdn:
            return cdn
        bucket = ICP.get_param("gohan.s3_bucket") or ""
        region = ICP.get_param("gohan.s3_region") or "us-east-1"
        if bucket:
            return f"https://{bucket}.s3.{region}.amazonaws.com"
        return ""

    def svg_markup(self):
        """Return the SVG markup for an SVG asset.

        Prefers ``content_text`` (extracted icons, and uploaded files that
        were auto-captured); falls back to decoding ``file_data``.
        """
        self.ensure_one()
        if self.content_text:
            return self.content_text
        if self.file_data:
            try:
                raw = self.file_data
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                return base64.b64decode(raw).decode("utf-8", "ignore")
            except Exception:
                return ""
        return ""

    @api.depends("asset_type", "s3_key", "file_data", "content_text", "mime_type")
    def _compute_preview_html(self):
        from markupsafe import Markup

        base = self._s3_base_url()
        box = (
            "display:inline-block;max-width:240px;max-height:170px;"
            "border:1px solid #dee2e6;border-radius:4px;padding:4px;"
            "background:#fff;overflow:hidden;"
        )
        for rec in self:
            html = ""
            if rec.asset_type == "svg":
                markup = rec.svg_markup()
                if markup:
                    html = (
                        '<div style="width:120px;height:120px;display:flex;'
                        "align-items:center;justify-content:center;border:"
                        "1px solid #dee2e6;border-radius:4px;background:#fff;"
                        f'padding:8px;">{markup}</div>'
                    )
                elif rec.s3_key and base:
                    # No inline markup captured — the extraction Lambda
                    # delivered the icon as a bare .svg file on S3 with no
                    # manifest. Render it straight from S3, like a screenshot.
                    src = f"{base}/{rec.s3_key}"
                    html = (
                        f'<a href="{src}" target="_blank">'
                        f'<img src="{src}" style="{box}" loading="lazy"/></a>'
                    )
            elif rec.asset_type in IMAGE_ASSET_TYPES:
                src = ""
                if rec.file_data:
                    raw = rec.file_data
                    b64 = raw.decode() if isinstance(raw, bytes) else raw
                    src = f"data:{rec.mime_type or 'image/png'};base64,{b64}"
                elif rec.s3_key and base:
                    src = f"{base}/{rec.s3_key}"
                if src:
                    html = (
                        f'<a href="{src}" target="_blank">'
                        f'<img src="{src}" style="{box}" loading="lazy"/></a>'
                    )
            if not html:
                label = dict(self._fields["asset_type"].selection).get(
                    rec.asset_type, "Asset"
                )
                html = (
                    f'<span class="text-muted fst-italic">{label} '
                    "(no preview)</span>"
                )
            rec.preview_html = Markup(html)

    # ------------------------------------------------------------------
    # SVG upload auto-capture
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_svg_autofill(vals, *, on_create):
        """Capture markup + type when an operator uploads an .svg file.

        Uploaded SVGs need their markup in ``content_text`` so PRD generation
        can feed the icon to Bedrock without an S3 round-trip.
        """
        fd = vals.get("file_data")
        if not fd:
            return
        fname = (vals.get("file_name") or "").lower()
        try:
            raw = fd if isinstance(fd, (bytes, bytearray)) else fd.encode("utf-8")
            text = base64.b64decode(raw).decode("utf-8", "ignore")
        except Exception:
            text = ""
        looks_svg = fname.endswith(".svg") or "<svg" in text[:600].lower()
        if not looks_svg:
            return
        if text and not vals.get("content_text"):
            vals["content_text"] = _sanitize_svg_markup(text)[:20000]
        vals.setdefault("mime_type", "image/svg+xml")
        if on_create:
            vals.setdefault("asset_type", "svg")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._apply_svg_autofill(vals, on_create=True)
            if vals.get("content_text"):
                vals["content_text"] = _sanitize_svg_markup(vals["content_text"])
            if not vals.get("name") and vals.get("file_name"):
                vals["name"] = vals["file_name"]
        return super().create(vals_list)

    def write(self, vals):
        if "file_data" in vals:
            self._apply_svg_autofill(vals, on_create=False)
        if vals.get("content_text"):
            vals["content_text"] = _sanitize_svg_markup(vals["content_text"])
        return super().write(vals)
