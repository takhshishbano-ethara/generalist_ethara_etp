# -*- coding: utf-8 -*-
"""SOP semantic tags — short, prefixed, kebab-case labels characterizing a
generator's task (e.g. ``task:pairwise-comparison``, ``domain:image-evaluation``).
Extracted from the SOP by an LLM (services/vertex.extract_tags_from_sop) and
stored PREFIXED so a later chunk can weight them by prefix for ranking."""
import re

from odoo import models, fields, api
from odoo.exceptions import ValidationError

# Keep to short semantic labels: lowercase letters/digits, single ':' prefix
# separator, '-' word separator. Everything else is stripped in _canonicalize.
_CANON_KEEP_RE = re.compile(r"[^a-z0-9:-]+")
_CANON_DASH_RE = re.compile(r"-{2,}")
_CANON_COLON_RE = re.compile(r":{2,}")


class EtpAssessmentTag(models.Model):
    _name = "etp.assessment.pro.tag"
    _description = "SOP Semantic Tag"
    _order = "name"

    name = fields.Char(required=True, index=True)          # "task:pairwise-comparison"
    prefix = fields.Char(compute="_compute_parts", store=True)  # "task"
    label = fields.Char(compute="_compute_parts", store=True)   # "pairwise-comparison"
    color = fields.Integer()

    @api.depends("name")
    def _compute_parts(self):
        for rec in self:
            raw = (rec.name or "").strip()
            if ":" in raw:
                prefix, label = raw.split(":", 1)
                rec.prefix = prefix
                rec.label = label
            else:
                rec.prefix = ""
                rec.label = raw

    @api.depends("name", "label")
    @api.depends_context("etp_hide_tag_prefix")
    def _compute_display_name(self):
        """Display-only prefix hiding for the GENERATOR views. The canonical
        ``name`` (``domain:image-evaluation``) is what the weighted-Jaccard
        ranking reads (via ``prefix`` + the M2M relation), never ``display_name``,
        so stripping the prefix here is purely cosmetic. Gated on the
        ``etp_hide_tag_prefix`` context flag (set on the generator views only) so
        every other place still shows the full faceted tag."""
        hide = self.env.context.get("etp_hide_tag_prefix")
        for rec in self:
            if hide and rec.label:
                rec.display_name = rec.label.replace("-", " ").title()
            else:
                rec.display_name = rec.name or ""

    @api.constrains("name")
    def _check_unique_name(self):
        """Case-insensitive unique name. Mirrors the module's @api.constrains
        uniqueness idiom (see question.dimension) rather than _sql_constraints,
        which is deprecated in Odoo 19."""
        for rec in self:
            if not rec.name:
                continue
            duplicate = self.search_count([
                ("id", "!=", rec.id),
                ("name", "=ilike", rec.name),
            ])
            if duplicate:
                raise ValidationError(
                    "Tag '%s' already exists." % rec.name)

    @api.model
    def _canonicalize(self, raw):
        """Normalize one raw tag string to the stored form: strip, lower,
        collapse whitespace to '-', keep only [a-z0-9:-], collapse repeated
        '-'/':'. Returns '' when nothing usable remains."""
        text = (raw or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"\s+", "-", text)
        text = _CANON_KEEP_RE.sub("", text)
        text = _CANON_DASH_RE.sub("-", text)
        text = _CANON_COLON_RE.sub(":", text)
        text = text.strip("-:")
        return text

    @api.model
    def _get_or_create(self, names):
        """Given raw tag strings, canonicalize + dedupe (case-insensitive) and
        return the matching recordset, creating any that don't yet exist. Blank
        / unusable entries are skipped."""
        result = self.browse()
        seen = set()
        for raw in (names or []):
            canon = self._canonicalize(raw)
            if not canon or canon in seen:
                continue
            seen.add(canon)
            tag = self.search([("name", "=ilike", canon)], limit=1)
            if not tag:
                tag = self.create({"name": canon})
            result |= tag
        return result
