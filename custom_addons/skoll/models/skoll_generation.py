from odoo import api, fields, models

PRICING = {
    "generate": (3.00, 15.00),
    "qc": (0.60, 3.00),
    "improve": (3.00, 15.00),
}


class SkollGeneration(models.Model):
    _name = "skoll.generation"
    _description = "Skoll Generation Log"
    _order = "create_date desc, id desc"

    call_type = fields.Selection(
        [("generate", "Generate"), ("qc", "QC Review"), ("improve", "Improve")],
        string="Type", default="generate", readonly=True, index=True,
    )

    task_id = fields.Many2one(
        "skoll.skoll", string="Task", ondelete="cascade", index=True,
    )
    task_ref = fields.Char(
        related="task_id.task_id", string="Task ID", store=True, index=True,
    )
    user_id = fields.Many2one(
        "res.users", string="User", default=lambda self: self.env.uid,
        index=True, readonly=True,
    )
    persona_id = fields.Many2one(
        related="task_id.persona_id", string="Persona", store=True, readonly=True,
    )

    input_tokens = fields.Integer(string="Input Tokens", readonly=True)
    output_tokens = fields.Integer(string="Output Tokens", readonly=True)
    total_tokens = fields.Integer(
        string="Total Tokens", compute="_compute_totals", store=True,
    )

    input_cost = fields.Float(
        string="Input Cost ($)", compute="_compute_totals", store=True, digits=(12, 6),
    )
    output_cost = fields.Float(
        string="Output Cost ($)", compute="_compute_totals", store=True, digits=(12, 6),
    )
    total_cost = fields.Float(
        string="Total Cost ($)", compute="_compute_totals", store=True, digits=(12, 6),
    )

    model_arn = fields.Char(string="Model ARN", readonly=True)
    duration_s = fields.Float(string="Duration (s)", readonly=True, digits=(10, 2))
    status = fields.Selection(
        [("success", "Success"), ("error", "Error")],
        string="Status", default="success", readonly=True,
    )
    error_message = fields.Text(string="Error", readonly=True)

    @api.depends("input_tokens", "output_tokens", "call_type")
    def _compute_totals(self):
        for rec in self:
            rec.total_tokens = rec.input_tokens + rec.output_tokens
            inp_rate, out_rate = PRICING.get(rec.call_type or "generate", PRICING["generate"])
            rec.input_cost = rec.input_tokens * inp_rate / 1_000_000
            rec.output_cost = rec.output_tokens * out_rate / 1_000_000
            rec.total_cost = rec.input_cost + rec.output_cost
