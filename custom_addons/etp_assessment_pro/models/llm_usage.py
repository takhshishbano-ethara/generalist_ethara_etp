# -*- coding: utf-8 -*-
"""LLM usage ledger — one row per LLM/image API call; written best-effort by services/vertex.py."""
from odoo import models, fields, api


class EtpLlmUsage(models.Model):
    _name = "etp.assessment.pro.llm.usage"
    _description = "LLM Call Usage / Token Ledger"
    _order = "create_date desc"
    _rec_name = "operation"

    operation = fields.Selection(
        [
            ("extract_skills", "Extract Skills"),
            ("extract_tags", "Extract Tags"),
            ("generate_questions", "Generate Questions"),
            ("generate_image", "Generate Image"),
            ("detect_image_elements", "Detect Image Elements"),
            ("verify_planted_flaws", "Verify Planted Flaws"),
            ("generate_video", "Generate Video"),
            ("submit_video_op", "Submit Video (Veo)"),
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
    video_seconds = fields.Float(
        string="Video Seconds", digits=(12, 2), default=0.0,
        help="Seconds of generated video (Veo) for this call; drives the "
             "per-second video cost. 0 for non-video calls.")
    cost_usd = fields.Float(
        string="Est. Cost (USD)", digits=(12, 5),
        help="Best-effort estimate from a per-model rate table in "
             "services/vertex.py; the token counts are exact.")
    prompt_id = fields.Many2one(
        "etp.assessment.pro.prompt", string="Generator",
        ondelete="set null", index=True)
    evaluator_id = fields.Many2one(
        "etp.assessment.pro.evaluator", string="Candidate",
        ondelete="set null")
    note = fields.Char(string="Note")

    @api.depends("tokens_in", "tokens_out", "thoughts_tokens")
    def _compute_total_tokens(self):
        for rec in self:
            rec.total_tokens = ((rec.tokens_in or 0) + (rec.tokens_out or 0)
                                + (rec.thoughts_tokens or 0))
