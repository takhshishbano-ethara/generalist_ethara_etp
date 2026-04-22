# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

COST_RATES = {
    "kimi": {"input": 1.00, "output": 3.00, "cached": 0.0},
    "claude": {"input": 3.00, "output": 15.00, "cached": 0.30},
    "gemini": {"input": 1.25, "output": 10.00, "cached": 0.30},
    "openai": {"input": 2.50, "output": 10.00, "cached": 1.25},
}


class BerserkerUsageLog(models.Model):
    _name = "berserker.usage.log"
    _description = "Berserker LLM Usage Log"
    _order = "create_date desc"

    provider = fields.Selection(
        [
            ("kimi", "Kimi (Bedrock)"),
            ("claude", "Claude (Bedrock)"),
            ("gemini", "Gemini"),
            ("openai", "GPT (OpenAI)"),
        ],
        required=True,
        index=True,
    )
    model_name = fields.Char(string="Model", index=True)
    call_type = fields.Selection(
        [
            ("response_gen", "Response Generation"),
            ("rubric_gen", "Rubric Generation"),
            ("evaluation", "Evaluation"),
            ("kimi_assist", "Kimi Assist"),
            ("qc", "QC Check"),
        ],
        required=True,
    )
    berserker_id = fields.Many2one(
        "berserker",
        string="Task",
        index=True,
        ondelete="set null",
    )
    employee_id = fields.Many2one(
        related="berserker_id.employee_id",
        string="Employee",
        store=True,
    )

    input_tokens = fields.Integer(string="Input Tokens", default=0)
    output_tokens = fields.Integer(string="Output Tokens", default=0)
    cached_tokens = fields.Integer(string="Cached Tokens", default=0)
    total_tokens = fields.Integer(
        string="Total Tokens",
        compute="_compute_total_tokens",
        store=True,
    )

    success = fields.Boolean(string="Success", default=True)
    error_message = fields.Text(string="Error")
    duration_seconds = fields.Float(string="Duration (s)", digits=(10, 2))

    cost_estimate = fields.Float(
        string="Cost ($)",
        compute="_compute_cost",
        store=True,
        digits=(10, 6),
    )

    @api.depends("input_tokens", "output_tokens", "cached_tokens")
    def _compute_total_tokens(self):
        for rec in self:
            rec.total_tokens = rec.input_tokens + rec.output_tokens

    @api.depends("provider", "input_tokens", "output_tokens", "cached_tokens")
    def _compute_cost(self):
        for rec in self:
            rates = COST_RATES.get(rec.provider, {})
            effective_input = max(rec.input_tokens - rec.cached_tokens, 0)
            rec.cost_estimate = (
                (effective_input * rates.get("input", 0) / 1_000_000)
                + (rec.output_tokens * rates.get("output", 0) / 1_000_000)
                + (rec.cached_tokens * rates.get("cached", 0) / 1_000_000)
            )
