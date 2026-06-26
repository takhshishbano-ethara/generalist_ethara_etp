from __future__ import annotations

from odoo import fields, models


class LynceusLLMCall(models.Model):
    _name = "lynceus.llm.call"
    _description = "Lynceus LLM Call (per-call telemetry: tokens, cost, raw response)"
    _order = "batch_id desc, sequence asc, id asc"
    _rec_name = "seed"

    batch_id = fields.Many2one(
        "lynceus.batch",
        string="Batch",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(string="Call #", default=0)
    model = fields.Char(string="Model", readonly=True)
    seed = fields.Char(string="Seed", readonly=True)
    requested_count = fields.Integer(string="Requested Prompts", readonly=True)
    returned_count = fields.Integer(string="Returned Prompts", readonly=True)
    input_tokens = fields.Integer(string="Input Tokens", readonly=True)
    output_tokens = fields.Integer(string="Output Tokens", readonly=True)
    thoughts_tokens = fields.Integer(
        string="Thoughts Tokens",
        readonly=True,
        help="Should be 0 when thinkingBudget=0 is honored. "
             "Non-zero here means the model ignored the thinking-budget setting.",
    )
    cost_usd = fields.Float(
        string="Cost (USD)",
        readonly=True,
        digits=(12, 6),
    )
    finish_reason = fields.Char(string="Finish Reason", readonly=True)
    parse_errors = fields.Text(string="Parse Errors", readonly=True)
    raw_response = fields.Text(
        string="Raw Response JSON",
        readonly=True,
        help="Full Vertex AI response body for this call. Administrators only.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
        index=True,
    )
