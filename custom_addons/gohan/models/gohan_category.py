from odoo import fields, models


class GohanCategory(models.Model):
    _name = "gohan.category"
    _description = "Website Category"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    code = fields.Char(
        string="Code",
        required=True,
        help=(
            "Canonical Gohan category code passed to the pipeline via "
            "--category and used in scoring/QC rules. One of: crm, erp, "
            "hcm, tms, news, publishing, retail, services, knowledge, "
            "procurement, vertical_markets, gov_portal, community, "
            "multimedia, ai_platform, public_utility."
        ),
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("code_unique", "UNIQUE(code)", "Category code must be unique."),
    ]
