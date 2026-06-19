from odoo import api, fields, models


class TrajectoryCostModelUsage(models.Model):
    _name = "trajectory.cost.model.usage"
    _description = "AI Trajectory Cost - Per-Model Usage"
    _order = "trajectory_cost_id, model_name"
    _rec_name = "model_name"

    trajectory_cost_id = fields.Many2one(
        "trajectory.cost",
        required=True,
        ondelete="cascade",
        index=True,
        string="Trajectory",
    )
    trajectory_id = fields.Char(
        related="trajectory_cost_id.trajectory_id",
        store=True,
        index=True,
        readonly=True,
        string="Trajectory ID",
    )
    project_key = fields.Char(
        related="trajectory_cost_id.project_key",
        store=True,
        index=True,
        readonly=True,
    )

    model_name = fields.Char(index=True, string="Model")
    input_tokens = fields.Float()
    output_tokens = fields.Float()
    cache_tokens = fields.Float()
    total_tokens = fields.Float(compute="_compute_total_tokens", store=True)
    cost = fields.Float(digits=(16, 6))

    _uniq_trajectory_model = models.Constraint(
        "unique(trajectory_cost_id, model_name)",
        "Only one usage row per model within a trajectory.",
    )

    @api.depends("input_tokens", "output_tokens", "cache_tokens")
    def _compute_total_tokens(self):
        for rec in self:
            rec.total_tokens = (
                (rec.input_tokens or 0.0)
                + (rec.output_tokens or 0.0)
                + (rec.cache_tokens or 0.0)
            )
