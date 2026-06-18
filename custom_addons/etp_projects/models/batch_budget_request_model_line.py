from odoo import api, fields, models


class EtpBatchBudgetRequestModelLine(models.Model):
    _name = "etp.batch.budget.request.model.line"
    _description = "Batch Budget Request Model Line"
    _order = "id"

    request_id = fields.Many2one(
        "etp.batch.budget.request",
        string="Request",
        required=True,
        ondelete="cascade",
        index=True,
    )
    ai_model_id = fields.Many2one(
        "etp.ai.model",
        string="Model",
        required=True,
    )
    description = fields.Char(string="Task Description")
    per_task_cost = fields.Float(string="Per Task Cost (USD)")
    requested_amount = fields.Float(
        string="Requested (USD)",
        compute="_compute_requested_amount",
        store=True,
        readonly=False,
        help="Total Tasks (on the request) x Per Task Cost.",
    )
    approved_amount = fields.Float(string="Approved (USD)")

    @api.depends("request_id.total_tasks", "per_task_cost")
    def _compute_requested_amount(self):
        for line in self:
            line.requested_amount = (
                (line.request_id.total_tasks or 0) * (line.per_task_cost or 0.0)
            )
