from odoo import api, fields, models


class EtpBatchBudgetRequestModelLine(models.Model):
    _name = "etp.batch.budget.request.model.line"
    _description = "Phase Budget Request Model Line"
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
    cost_type = fields.Selection(
        [
            ("per_task", "Per Task"),
            ("per_trajectory", "Per Trajectory"),
        ],
        string="Cost Type",
        default="per_task",
        required=True,
    )
    per_trajectory_cost = fields.Float(string="Per Trajectory Cost (USD)")
    iterations = fields.Integer(string="No. of Trajectories per Task")
    per_task_cost = fields.Float(string="Per Task Cost (USD)")
    requested_amount = fields.Float(
        string="Requested (USD)",
        help="Auto-suggested as Total Tasks (on the request) x Per Task Cost "
             "whenever Per Task Cost changes. Fully editable.",
    )
    approved_amount = fields.Float(string="Approved (USD)")

    @api.onchange("cost_type", "per_trajectory_cost", "iterations")
    def _onchange_trajectory_inputs(self):
        for line in self:
            if line.cost_type == "per_trajectory":
                line.per_task_cost = (
                    (line.per_trajectory_cost or 0.0)
                    * (line.iterations or 0)
                )
                if line.request_id:
                    line.requested_amount = (
                        (line.request_id.total_tasks or 0)
                        * (line.per_task_cost or 0.0)
                    )

    @api.onchange("per_task_cost")
    def _onchange_per_task_cost(self):
        for line in self:
            if line.request_id:
                line.requested_amount = (
                    (line.request_id.total_tasks or 0)
                    * (line.per_task_cost or 0.0)
                )
