from odoo import api, fields, models


class ErzaModel(models.Model):
    _name = "erza.model"
    _description = "Erza Evaluated Model"
    _order = "sequence, name"

    key = fields.Char(required=True, index=True, help="Machine identifier, e.g. claude-opus-4-8.")
    name = fields.Char(required=True)
    provider = fields.Char()
    provider_route = fields.Char(help="Fully-qualified model route, e.g. vertex_ai/claude-opus-4-8.")
    color = fields.Char(default="#845EF7")
    sequence = fields.Integer(default=10)

    run_ids = fields.One2many("erza.run", "model_id")
    run_count = fields.Integer(compute="_compute_stats", store=True)
    pass_count = fields.Integer(compute="_compute_stats", store=True)
    pass_at_3 = fields.Float(string="With-Skills Pass (%)", compute="_compute_stats", store=True, aggregator="avg")
    no_skill_pass_rate = fields.Float(string="No-Skills Pass (%)", compute="_compute_stats", store=True, aggregator="avg")
    mean_delta = fields.Float(string="Mean Δ (pp)", compute="_compute_stats", store=True, aggregator="avg")
    mean_score = fields.Float(string="With-Skills Mean Score (%)", compute="_compute_stats", store=True, aggregator="avg")
    total_cost_usd = fields.Float(string="Total Cost (USD)", compute="_compute_stats", store=True)

    _sql_constraints = [
        ("erza_model_key_uniq", "unique(key)", "Model key must be unique."),
    ]

    @api.depends("run_ids", "run_ids.score", "run_ids.score_binary",
                 "run_ids.no_skill_binary", "run_ids.delta", "run_ids.cost_usd")
    def _compute_stats(self):
        for rec in self:
            runs = rec.run_ids
            total = len(runs)
            passed = sum(1 for r in runs if r.score_binary)
            ns_passed = sum(1 for r in runs if r.no_skill_binary)
            rec.run_count = total
            rec.pass_count = passed
            rec.pass_at_3 = (passed / total * 100.0) if total else 0.0
            rec.no_skill_pass_rate = (ns_passed / total * 100.0) if total else 0.0
            rec.mean_delta = (sum(r.delta for r in runs) / total * 100.0) if total else 0.0
            rec.mean_score = (sum(r.score for r in runs) / total * 100.0) if total else 0.0
            rec.total_cost_usd = sum(r.cost_usd for r in runs)
