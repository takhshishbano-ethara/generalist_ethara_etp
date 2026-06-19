from odoo import api, fields, models


class TrajectoryCost(models.Model):
    _name = "trajectory.cost"
    _description = "AI Trajectory Cost"
    _order = "create_date desc, id desc"
    _rec_name = "trajectory_id"

    project_key = fields.Char(index=True)
    trajectory_id = fields.Char(index=True)

    model_usage_ids = fields.One2many(
        "trajectory.cost.model.usage",
        "trajectory_cost_id",
        string="Per-Model Usage",
    )

    input_tokens = fields.Float(
        compute="_compute_totals", store=True, string="Input Tokens",
    )
    output_tokens = fields.Float(
        compute="_compute_totals", store=True, string="Output Tokens",
    )
    cache_tokens = fields.Float(
        compute="_compute_totals", store=True, string="Cache Tokens",
    )
    total_tokens = fields.Float(
        compute="_compute_totals", store=True, string="Total Tokens",
    )
    cost = fields.Float(
        compute="_compute_totals", store=True, digits=(16, 6), string="Cost",
    )
    model_count = fields.Integer(
        compute="_compute_totals", store=True, string="Models",
    )

    @api.depends(
        "model_usage_ids.input_tokens",
        "model_usage_ids.output_tokens",
        "model_usage_ids.cache_tokens",
        "model_usage_ids.cost",
    )
    def _compute_totals(self):
        for rec in self:
            rec.input_tokens = sum(rec.model_usage_ids.mapped("input_tokens"))
            rec.output_tokens = sum(rec.model_usage_ids.mapped("output_tokens"))
            rec.cache_tokens = sum(rec.model_usage_ids.mapped("cache_tokens"))
            rec.cost = sum(rec.model_usage_ids.mapped("cost"))
            rec.total_tokens = (
                (rec.input_tokens or 0.0)
                + (rec.output_tokens or 0.0)
                + (rec.cache_tokens or 0.0)
            )
            rec.model_count = len(rec.model_usage_ids)
