from odoo import api, fields, models


class EtpBatchBudgetRequestInfraLine(models.Model):
    _name = "etp.batch.budget.request.infra.line"
    _description = "Phase Budget Request Infrastructure Line"
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
    start_date = fields.Date(string="Start Date")
    end_date = fields.Date(string="End Date")
    per_day_requested = fields.Float(
        string="Per Day Requested (USD)",
        compute="_compute_per_day_amounts",
        store=True,
    )
    per_day_approved = fields.Float(
        string="Per Day Approved (USD)",
        compute="_compute_per_day_amounts",
        store=True,
    )

    @api.depends("requested_amount", "approved_amount")
    def _compute_per_day_amounts(self):
        for rec in self:
            rec.per_day_requested = (rec.requested_amount or 0.0) / 30.0
            rec.per_day_approved = (rec.approved_amount or 0.0) / 30.0
