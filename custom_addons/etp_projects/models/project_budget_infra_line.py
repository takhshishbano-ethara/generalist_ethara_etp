from odoo import fields, models


class EtpProjectBudgetInfraLine(models.Model):
    _name = "etp.project.budget.infra.line"
    _description = "Project Budget Infrastructure Line"
    _order = "id"

    budget_id = fields.Many2one(
        "etp.project.aws.budget",
        string="Project Budget",
        required=True,
        ondelete="cascade",
        index=True,
    )
    infra_type_id = fields.Many2one(
        "etp.infra.type",
        string="Infrastructure",
        required=True,
    )
    description = fields.Char(string="Description")
    budget_amount = fields.Float(string="Budget (USD)")
