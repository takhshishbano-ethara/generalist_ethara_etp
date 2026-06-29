from odoo import api, fields, models


class EtpProjectBudgetModelLine(models.Model):
    _name = "etp.project.budget.model.line"
    _description = "Project Budget Model Line"
    _order = "id"

    budget_id = fields.Many2one(
        "etp.project.aws.budget",
        string="Project Budget",
        required=True,
        ondelete="cascade",
        index=True,
    )
    ai_model_id = fields.Many2one(
        "etp.ai.model",
        string="Model",
        required=True,
    )
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

    @api.onchange("cost_type", "per_trajectory_cost", "iterations")
    def _onchange_trajectory_inputs(self):
        for line in self:
            if line.cost_type == "per_trajectory":
                line.per_task_cost = (
                    (line.per_trajectory_cost or 0.0)
                    * (line.iterations or 0)
                )
