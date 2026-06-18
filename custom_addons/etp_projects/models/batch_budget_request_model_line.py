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
    task_count = fields.Integer(string="Task Count", default=0)
    per_task_cost = fields.Float(string="Per Task Cost (USD)")
    requested_amount = fields.Float(
        string="Requested (USD)",
        compute="_compute_requested_amount",
        store=True,
        readonly=False,
        help="Defaults to task_count * per_task_cost; override directly if needed.",
    )
    approved_amount = fields.Float(string="Approved (USD)")

    @api.depends("task_count", "per_task_cost")
    def _compute_requested_amount(self):
        for line in self:
            if line.task_count and line.per_task_cost:
                line.requested_amount = line.task_count * line.per_task_cost
            elif not line.requested_amount:
                line.requested_amount = 0.0
