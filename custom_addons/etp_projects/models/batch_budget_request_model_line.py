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
        help="Auto-suggested as Total Tasks (on the request) x Per Task Cost "
             "whenever Per Task Cost changes. Fully editable.",
    )
    approved_amount = fields.Float(string="Approved (USD)")

    @api.onchange("per_task_cost")
    def _onchange_per_task_cost(self):
        for line in self:
            if line.request_id:
                line.requested_amount = (
                    (line.request_id.total_tasks or 0)
                    * (line.per_task_cost or 0.0)
                )
