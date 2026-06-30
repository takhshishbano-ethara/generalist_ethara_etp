from odoo import api, fields, models


class EtpBatchBudgetInfraLine(models.Model):
    _name = "etp.batch.budget.infra.line"
    _description = "Phase Budget Infrastructure Line"
    _order = "id"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Phase Budget",
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
    start_date = fields.Date(string="Start Date")
    end_date = fields.Date(string="End Date")
    per_day_cost = fields.Float(
        string="Per Day Cost (USD)",
        compute="_compute_per_day_cost",
        store=True,
    )

    @api.depends("budget_amount")
    def _compute_per_day_cost(self):
        for rec in self:
            rec.per_day_cost = (rec.budget_amount or 0.0) / 30.0
