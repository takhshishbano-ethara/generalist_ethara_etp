import re
import string

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class FenrirCategory(models.Model):
    _name = "fenrir.category"
    _description = "Fenrir Task Category"
    _order = "sequence, name"

    name = fields.Char(string="Name", required=True)
    sequence = fields.Integer(default=10)
    code = fields.Char(string="Short Code",
                       help="Optional short code, e.g. 'GDV' for Game Development")
    description = fields.Text(string="Description")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("fenrir_category_name_unique", "unique(name)",
         "Category name must be unique."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name"):
                vals["name"] = vals["name"].strip()
            code = (vals.get("code") or "").strip().upper()
            if code:
                vals["code"] = code
            elif vals.get("name"):
                # No code supplied — autogenerate a unique one from the name.
                vals["code"] = self._generate_unique_code(vals["name"])
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("name"):
            vals["name"] = vals["name"].strip()
        if vals.get("code"):
            vals["code"] = vals["code"].strip().upper()
        return super().write(vals)

    # ── Code generation ──────────────────────────────────────────────────
    @api.model
    def _make_code_candidates(self, name):
        """Ordered candidate 2–3 letter codes derived from a category name.

        Starts with the first 2 letters, then the first 3, then systematic
        combinations, so a unique code can always be found even when several
        categories share a prefix (e.g. 'Artificial…' and 'Architecture')."""
        letters = re.sub(r"[^A-Za-z]", "", name or "").upper()
        if not letters:
            letters = "XX"
        out = []

        def add(cand):
            if 2 <= len(cand) <= 3 and cand not in out:
                out.append(cand)

        add(letters[:2].ljust(2, "X"))       # first 2 letters (spec default)
        if len(letters) >= 3:
            add(letters[:3])                  # first 3 letters
        for ch in letters[1:]:                # first letter + a later one
            add(letters[0] + ch)
        for ch in letters[2:]:                # first two + a later one
            add(letters[:2] + ch)
        for a in string.ascii_uppercase:      # brute-force 2-letter fallback
            for b in string.ascii_uppercase:
                add(a + b)
        return out

    @api.model
    def _generate_unique_code(self, name):
        """Return the first candidate code not already used (case-insensitive)."""
        for cand in self._make_code_candidates(name):
            if not self.with_context(active_test=False).search_count(
                    [("code", "=ilike", cand)]):
                return cand
        # Practically unreachable (676 two-letter combos); last-ditch value.
        return re.sub(r"[^A-Za-z]", "", name or "")[:3].upper() or "XX"

    def _ensure_code(self):
        """Return this category's code, generating and storing one if missing."""
        self.ensure_one()
        if not self.code:
            self.code = self._generate_unique_code(self.name)
        return self.code

    @api.constrains("code")
    def _check_code_format(self):
        for rec in self:
            if rec.code and not re.match(r"^[A-Za-z0-9]{2,3}$", rec.code):
                raise ValidationError(
                    f"Category code '{rec.code}' must be 2–3 letters and/or "
                    f"numbers (A–Z, 0–9)."
                )

    @api.constrains("name")
    def _check_name_unique_ci(self):
        for rec in self:
            if not rec.name:
                continue
            duplicate = self.with_context(active_test=False).search([
                ("id", "!=", rec.id),
                ("name", "=ilike", rec.name),
            ], limit=1)
            if duplicate:
                raise ValidationError(
                    f"A category named '{duplicate.name}' already exists "
                    f"(case-insensitive match). Pick a different name."
                )

    @api.constrains("code")
    def _check_code_unique_ci(self):
        for rec in self:
            if not rec.code:
                continue
            duplicate = self.with_context(active_test=False).search([
                ("id", "!=", rec.id),
                ("code", "=ilike", rec.code),
            ], limit=1)
            if duplicate:
                raise ValidationError(
                    f"Category code '{duplicate.code}' is already in use."
                )
