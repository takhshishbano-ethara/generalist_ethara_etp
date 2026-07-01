from odoo import api, fields, models


class EtpBatchBudgetModelLine(models.Model):
    _name = "etp.batch.budget.model.line"
    _description = "Phase Budget Model Line"
    _order = "id"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Phase Budget",
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
    approved_amount = fields.Float(
        string="Approved (USD)",
        help="Sum of approved amounts from request lines targeting this model.",
    )
    consumed_amount = fields.Float(
        string="Consumed (USD)",
        compute="_compute_consumed_amount",
        help="LLM cost incurred by this model within the phase date range.",
    )
    remaining_amount = fields.Float(
        string="Remaining (USD)",
        compute="_compute_consumed_amount",
    )

    @api.onchange("cost_type", "per_trajectory_cost", "iterations")
    def _onchange_trajectory_inputs(self):
        for line in self:
            if line.cost_type == "per_trajectory":
                line.per_task_cost = (
                    (line.per_trajectory_cost or 0.0)
                    * (line.iterations or 0)
                )

    @api.depends(
        "approved_amount",
        "ai_model_id",
        "batch_id.start_date",
        "batch_id.end_date",
        "batch_id.project_id",
    )
    def _compute_consumed_amount(self):
        Line = self.env["etp.project.aws.cost.line"]
        for rec in self:
            batch = rec.batch_id
            project = batch.project_id if batch else False
            start_date = batch.start_date if batch else False
            end_date = batch.end_date if batch else False
            consumed = 0.0
            if (
                project
                and start_date
                and end_date
                and end_date >= start_date
                and rec.ai_model_id
                and rec.ai_model_id.name
            ):
                consumed = sum(
                    Line.sudo().search([
                        ("project_id", "=", project.id),
                        ("granularity", "=", "day"),
                        ("period", ">=", start_date),
                        ("period", "<=", end_date),
                        ("model_name", "=", rec.ai_model_id.name),
                    ]).mapped("amount_source")
                )
            rec.consumed_amount = consumed
            rec.remaining_amount = (rec.approved_amount or 0.0) - consumed
