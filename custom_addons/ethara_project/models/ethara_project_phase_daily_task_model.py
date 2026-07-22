from odoo import api, fields, models


HEALTH_SELECTION = [
    ("healthy", "Healthy"),
    ("warning", "Warning"),
    ("critical", "Critical"),
    ("unknown", "Unknown"),
]

WARNING_RATIO = 1.0
CRITICAL_RATIO = 1.1


class EtharaProjectPhaseDailyTaskModel(models.Model):
    _name = "ethara.project.phase.daily.task.model"
    _description = "Ethara Project Phase Daily Task - Model Breakdown"
    _order = "daily_task_id, id"

    daily_task_id = fields.Many2one(
        "ethara.project.phase.daily.task",
        string="Daily Task",
        required=True,
        ondelete="cascade",
        index=True,
    )
    ai_model_id = fields.Many2one(
        "ethara.project.ai.model",
        string="Provider",
        required=True,
    )
    ai_model_name = fields.Char(string="Model")
    cost_type = fields.Selection(
        [
            ("per_task", "Per Task"),
            ("per_trajectory", "Per Trajectory"),
        ],
        string="Cost Type",
        default="per_task",
    )
    per_task_cost = fields.Float(string="Per Task Cost (USD)")
    per_trajectory_cost = fields.Float(string="Per Trajectory Cost (USD)")
    iterations = fields.Integer(string="Trajectories per Task")

    # Per-row detail captured from the "Log daily task" popup's manual input
    # table. Populated only when the client sends `model_rows`; the phase's
    # auto-seeded breakdown leaves them empty.
    task_label = fields.Char(string="Task")
    runs = fields.Char(string="Runs")
    input_tokens = fields.Float(string="Input Tokens")
    output_tokens = fields.Float(string="Output Tokens")
    line_cost = fields.Float(string="Row Cost (USD)")

    done_count = fields.Integer(
        string="Done Tasks",
        related="daily_task_id.done_count",
        store=True,
    )
    trajectory_count = fields.Integer(
        string="Trajectories",
        compute="_compute_trajectory_count",
        store=True,
    )
    ideal_cost = fields.Float(
        string="Ideal Cost (USD)",
        compute="_compute_ideal_cost",
        store=True,
    )
    allocated_cost = fields.Float(
        string="Allocated Cost (USD)",
        compute="_compute_allocated_cost",
        store=True,
    )
    variance = fields.Float(
        string="Variance (USD)",
        compute="_compute_variance",
        store=True,
    )
    health_status = fields.Selection(
        HEALTH_SELECTION,
        string="Health",
        compute="_compute_health",
        store=True,
        default="unknown",
    )

    @api.depends("done_count", "iterations")
    def _compute_trajectory_count(self):
        for rec in self:
            rec.trajectory_count = (rec.done_count or 0) * (rec.iterations or 0)

    @api.depends("done_count", "per_task_cost")
    def _compute_ideal_cost(self):
        for rec in self:
            rec.ideal_cost = (rec.done_count or 0) * (rec.per_task_cost or 0.0)

    @api.depends(
        "ideal_cost",
        "daily_task_id.total_cost",
        "daily_task_id.model_breakdown_ids.ideal_cost",
    )
    def _compute_allocated_cost(self):
        for rec in self:
            siblings = rec.daily_task_id.model_breakdown_ids
            total_ideal = sum(siblings.mapped("ideal_cost"))
            total_cost = rec.daily_task_id.total_cost or 0.0
            if total_ideal:
                rec.allocated_cost = total_cost * (rec.ideal_cost / total_ideal)
            else:
                rec.allocated_cost = 0.0

    @api.depends("allocated_cost", "ideal_cost")
    def _compute_variance(self):
        for rec in self:
            rec.variance = (rec.allocated_cost or 0.0) - (rec.ideal_cost or 0.0)

    @api.depends("allocated_cost", "ideal_cost")
    def _compute_health(self):
        for rec in self:
            ideal = rec.ideal_cost or 0.0
            actual = rec.allocated_cost or 0.0
            if not ideal or not actual:
                rec.health_status = "unknown"
                continue
            ratio = actual / ideal
            if ratio <= WARNING_RATIO:
                rec.health_status = "healthy"
            elif ratio <= CRITICAL_RATIO:
                rec.health_status = "warning"
            else:
                rec.health_status = "critical"
