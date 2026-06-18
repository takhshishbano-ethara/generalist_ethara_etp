from odoo import fields, models


class EtpBatchBudgetRequestInfraLine(models.Model):
    _name = "etp.batch.budget.request.infra.line"
    _description = "Batch Budget Request Infrastructure Line"
    _order = "id"

    request_id = fields.Many2one(
        "etp.batch.budget.request",
        string="Request",
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
    requested_amount = fields.Float(string="Requested (USD)")
    approved_amount = fields.Float(string="Approved (USD)")
