# -*- coding: utf-8 -*-
"""LLM usage ledger — one row per LLM/image API call in the assessment flow.

Records tokens in/out (and the Gemini "thinking" tokens), image count, the
operation that made the call, the model, and an ESTIMATED USD cost, so admins
can size the budget of skill extraction, question generation, image generation
and subjective scoring. Written best-effort by services/vertex.py (a logging
failure never breaks a generation/scoring run).
"""
from odoo import models, fields, api


class EtpLlmUsage(models.Model):
    _name = "etp.assessment.pro.llm.usage"
    _description = "LLM Call Usage / Token Ledger"
    _order = "create_date desc"
    _rec_name = "operation"

    operation = fields.Selection(
        [
            ("extract_skills", "Extract Skills"),
            ("generate_questions", "Generate Questions"),
            ("generate_image", "Generate Image"),
            ("score_subjective", "Score Subjective"),
            ("other", "Other"),
        ],
        string="Operation", required=True, default="other", index=True)
    model = fields.Char(string="Model", index=True)
    tokens_in = fields.Integer(string="Tokens In")
    tokens_out = fields.Integer(string="Tokens Out")
    thoughts_tokens = fields.Integer(
        string="Thinking Tokens",
        help="Gemini 2.5/3 hidden reasoning tokens, billed as output.")
    total_tokens = fields.Integer(
        string="Total Tokens", compute="_compute_total_tokens", store=True)
    image_count = fields.Integer(string="Images")
    cost_usd = fields.Float(
        string="Est. Cost (USD)", digits=(12, 5),
        help="Best-effort estimate from a per-model rate table in "
             "services/vertex.py; the token counts are exact.")
    # Optional context links (set null on delete so the ledger survives).
    prompt_id = fields.Many2one(
        "etp.assessment.pro.prompt", string="Generator",
        ondelete="set null", index=True)
    skill_id = fields.Many2one(
        "etp.assessment.pro.skill", string="Skill", ondelete="set null")
    evaluator_id = fields.Many2one(
        "etp.assessment.pro.evaluator", string="Candidate",
        ondelete="set null")
    note = fields.Char(string="Note")

    @api.depends("tokens_in", "tokens_out", "thoughts_tokens")
    def _compute_total_tokens(self):
        for rec in self:
            rec.total_tokens = ((rec.tokens_in or 0) + (rec.tokens_out or 0)
                                + (rec.thoughts_tokens or 0))
