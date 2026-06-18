from odoo import fields, models


class EtpBatchBudgetInfraLine(models.Model):
    _name = "etp.batch.budget.infra.line"
    _description = "Batch Budget Infrastructure Line"
    _order = "id"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Batch Budget",
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
