from odoo import fields, models


class LeviathanCategory(models.Model):
    _name = "leviathan.category"
    _description = "Website Category"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    technical_key = fields.Char(
        string="Technical Key",
        required=True,
        help="Used internally for scoring rules (e.g., normal_website, not_applicable)",
    )
    active = fields.Boolean(default=True)
