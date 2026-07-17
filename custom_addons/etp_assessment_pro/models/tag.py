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
    # Human-readable, growth-facing alias PINNED to this key (2-5 plain words,
    # Title Case, no facet prefix). Minted by the extraction LLM the first time a
    # key is coined; reused verbatim on every later SOP that reuses the key. The
    # ranker NEVER reads this; it reads name/prefix only.
    display = fields.Char(
        string="Readable Name",
        help="Plain-English label a non-technical teammate reads (e.g. "
             "'Compare Two AI Images (A/B)'). One key maps to one readable name, "
             "forever. Ranking still uses the technical key; this is display only.")
    color = fields.Integer()
    usage_count = fields.Integer(
        string="Used in Projects", compute="_compute_usage_count",
        help="How many question generators (projects) reference this tag. "
             "Drives ranking popularity; higher = more central to the vocabulary.")

    def _compute_usage_count(self):
        field = self.env["etp.assessment.pro.prompt"]._fields["tag_ids"]
        rel, col_prompt, col_tag = (
            field.relation, field.column1, field.column2)
        counts = {}
        if self.ids:
            self.env.cr.execute(
                "SELECT {col_tag} AS tid, COUNT(DISTINCT {col_prompt}) AS c "
                "FROM {rel} WHERE {col_tag} IN %s GROUP BY {col_tag}".format(
                    rel=rel, col_prompt=col_prompt, col_tag=col_tag),
                (tuple(self.ids),))
            counts = {row[0]: row[1] for row in self.env.cr.fetchall()}
        for rec in self:
            rec.usage_count = int(counts.get(rec.id, 0))

    def action_merge_tags(self):
        """Merge every selected tag INTO the one with the highest usage (or the
        first by name on a tie): repoint all project (prompt.tag_ids) references
        onto the survivor, then delete the losers. Data-driven, human-triggered
        counterpart to the LLM ``consolidate_vocabulary`` pass — so a growth
        teammate can hand-merge drift another project can then reuse. Purely a
        DB operation; makes NO Vertex call."""
        if len(self) < 2:
            raise ValidationError(
                "Select at least two tags to merge.")
        ordered = self.sorted(
            key=lambda t: (-(t.usage_count or 0), t.name or ""))
        survivor = ordered[0]
        losers = ordered[1:]
        Prompt = self.env["etp.assessment.pro.prompt"].sudo()
        moved = 0
        for loser in losers:
            refs = Prompt.search([("tag_ids", "in", loser.id)])
            for p in refs:
                p.write({"tag_ids": [(4, survivor.id), (3, loser.id)]})
                moved += 1
            loser.unlink()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Tags Merged",
                "message": "Merged %d tag(s) into '%s'. %d project link(s) "
                           "repointed." % (len(losers),
                                           survivor.display or survivor.name,
                                           moved),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

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

    @api.depends("name", "label", "display")
    @api.depends_context("etp_hide_tag_prefix")
    def _compute_display_name(self):
        """Display-only: ranking reads ``name``/``prefix``, never ``display_name``.

        Prefers the pinned readable ``display`` when the caller asked to hide the
        prefix (growth-facing surfaces). Falls back to a humanized label only when
        no display has been minted yet, and to the raw key when the prefix must be
        shown (power/debug surfaces)."""
        hide = self.env.context.get("etp_hide_tag_prefix")
        for rec in self:
            if hide:
                if rec.display:
                    rec.display_name = rec.display
                elif rec.label:
                    rec.display_name = rec.label.replace("-", " ").title()
                else:
                    rec.display_name = rec.name or ""
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
    def _default_display(self, key):
        """Best-effort readable fallback from a key, used ONLY when the LLM did
        not supply a display (older rows / manual tags). Title-cases the label."""
        raw = (key or "").strip()
        label = raw.split(":", 1)[1] if ":" in raw else raw
        return label.replace("-", " ").title() if label else raw

    @api.model
    def _facet_vocabulary(self, limit_per_facet=40):
        """Frequency-ranked existing vocabulary fed back to the extraction LLM so
        runs CONVERGE on popular values instead of minting near-duplicates.

        Returns ``{facet: [{"value","display","count"}, ...]}`` sorted by usage
        count descending. ``count`` = number of prompts referencing the tag
        (the real popularity signal), computed data-drivenly from the m2m — no
        hardcoded synonym map anywhere.
        """
        field = self.env["etp.assessment.pro.prompt"]._fields["tag_ids"]
        rel, col_prompt, col_tag = (
            field.relation, field.column1, field.column2)
        # usage count per tag id, in one query
        counts = {}
        try:
            self.env.cr.execute(
                "SELECT {col_tag} AS tid, COUNT(DISTINCT {col_prompt}) AS c "
                "FROM {rel} GROUP BY {col_tag}".format(
                    rel=rel, col_prompt=col_prompt, col_tag=col_tag))
            counts = {row[0]: row[1] for row in self.env.cr.fetchall()}
        except Exception:  # noqa: BLE001 - fall back to unweighted on any DB shape surprise
            counts = {}
        vocab = {f: [] for f in _KNOWN_FACETS}
        for tag in self.search([]):
            name = tag.name or ""
            if ":" not in name:
                continue
            prefix, value = name.split(":", 1)
            if prefix not in vocab or not value:
                continue
            vocab[prefix].append({
                "value": value,
                "display": tag.display or self._default_display(name),
                "count": int(counts.get(tag.id, 0)),
            })
        out = {}
        for facet, items in vocab.items():
            if not items:
                continue
            items.sort(key=lambda d: (-d["count"], d["value"]))
            out[facet] = items[:limit_per_facet]
        return out

    @api.model
    def _get_or_create(self, names):
        """Accepts a list of either bare key strings (legacy) or
        ``{"key": ..., "display": ...}`` dicts (new tag schema). Sets ``display``
        on FIRST create only, never overwriting an existing pin (preserves the
        one-key-one-display invariant the ranker convergence depends on)."""
        result = self.browse()
        seen = set()
        for raw in (names or []):
            if isinstance(raw, dict):
                key = raw.get("key") or raw.get("name") or ""
                display = (raw.get("display") or "").strip()
            else:
                key = raw
                display = ""
            canon = self._canonicalize(key)
            if not canon or canon in seen:
                continue
            seen.add(canon)
            tag = self.search([("name", "=ilike", canon)], limit=1)
            if not tag:
                tag = self.create({
                    "name": canon,
                    "display": display or self._default_display(canon),
                })
            elif display and (not tag.display
                              or tag.display == self._default_display(canon)):
                # Pin a real readable display: set it when missing, OR upgrade a
                # bare auto-backfill (Title-Case of the key) to the LLM's phrase.
                # Never clobber a display that already differs from the fallback
                # (that one was deliberately minted) — preserves the 1:1 pin.
                tag.display = display
            result |= tag
        return result
