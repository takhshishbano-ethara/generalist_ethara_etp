# -*- coding: utf-8 -*-
import re

from odoo import models, fields, api
from odoo.exceptions import ValidationError

_CANON_KEEP_RE = re.compile(r"[^a-z0-9:-]+")
_CANON_DASH_RE = re.compile(r"-{2,}")
_CANON_COLON_RE = re.compile(r":{2,}")

# Must stay in sync with the facet prefixes used by the SOP extraction prompt.
_KNOWN_FACETS = ("domain", "task", "modality", "output-format", "skill")


class EtpAssessmentTag(models.Model):
    _name = "etp.assessment.pro.tag"
    _description = "SOP Semantic Tag"
    _order = "name"

    name = fields.Char(required=True, index=True)
    prefix = fields.Char(compute="_compute_parts", store=True)
    label = fields.Char(compute="_compute_parts", store=True)
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
        """Display-only: ranking reads ``name``/``prefix``, never ``display_name``."""
        hide = self.env.context.get("etp_hide_tag_prefix")
        for rec in self:
            if hide and rec.label:
                rec.display_name = rec.label.replace("-", " ").title()
            else:
                rec.display_name = rec.name or ""

    @api.constrains("name")
    def _check_unique_name(self):
        """Case-insensitive uniqueness; not _sql_constraints (deprecated in Odoo 19)."""
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
    def _facet_vocabulary(self, limit_per_facet=40):
        vocab = {f: set() for f in _KNOWN_FACETS}
        for name in self.search([]).mapped("name"):
            if not name or ":" not in name:
                continue
            prefix, value = name.split(":", 1)
            if prefix in vocab and value:
                vocab[prefix].add(value)
        return {f: sorted(vals)[:limit_per_facet]
                for f, vals in vocab.items() if vals}

    @api.model
    def _get_or_create(self, names):
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
