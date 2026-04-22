# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ── Kimi (Bedrock) ──────────────────────────────────────────
    berserker_kimi_arn = fields.Char(
        string="Kimi Bedrock ARN",
        config_parameter="berserker.kimi_arn",
        default="arn:aws:bedrock:ap-south-1:426628337772:application-inference-profile/pnymm9v4duzh",
    )
    berserker_kimi_region = fields.Char(
        string="Kimi Bedrock Region",
        config_parameter="berserker.kimi_region",
        default="ap-south-1",
    )

    # ── Claude (Bedrock) ────────────────────────────────────────
    berserker_claude_arn = fields.Char(
        string="Claude Bedrock ARN",
        config_parameter="berserker.claude_arn",
        default="arn:aws:bedrock:ap-south-1:426628337772:application-inference-profile/claude-4-7",
    )
    berserker_claude_region = fields.Char(
        string="Claude Bedrock Region",
        config_parameter="berserker.claude_region",
        default="ap-south-1",
    )

    # ── Gemini ──────────────────────────────────────────────────
    berserker_gemini_model = fields.Char(
        string="Gemini Model Name",
        config_parameter="berserker.gemini_model",
        default="gemini-3.1-pro-preview",
    )

    # ── OpenAI ──────────────────────────────────────────────────
    berserker_openai_model = fields.Char(
        string="OpenAI Model Name",
        config_parameter="berserker.openai_model",
        default="gpt-5.4",
    )

    def set_values(self):
        res = super().set_values()
        from ..controllers.llm_actions import invalidate_config_cache

        invalidate_config_cache()
        return res
