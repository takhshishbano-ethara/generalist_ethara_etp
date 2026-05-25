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
            if vals.get("code"):
                vals["code"] = vals["code"].strip()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("name"):
            vals["name"] = vals["name"].strip()
        if vals.get("code"):
            vals["code"] = vals["code"].strip()
        return super().write(vals)

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
