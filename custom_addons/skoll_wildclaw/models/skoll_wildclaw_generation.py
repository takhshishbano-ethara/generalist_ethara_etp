from odoo import fields, models


class SkollWildclawGeneration(models.Model):
    _name = "skoll_wildclaw.generation"
    _description = "Skoll WildClaw Generation (cost tracking)"
    _order = "create_date desc"

    task_id = fields.Many2one("skoll_wildclaw.task", required=True, ondelete="cascade", index=True)
    call_type = fields.Selection(
        [("generate", "Generate"), ("qc", "QC"), ("improve", "Improve")],
        required=True,
    )
    model = fields.Char()
    input_tokens = fields.Integer()
    output_tokens = fields.Integer()
    cache_read_tokens = fields.Integer()
    total_tokens = fields.Integer()
    cost_usd = fields.Float(digits=(12, 6))
    duration_ms = fields.Integer()
    status = fields.Selection(
        [("pending", "Pending"), ("running", "Running"), ("done", "Done"), ("error", "Error")],
        default="pending",
    )
    error = fields.Text()
