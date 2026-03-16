# -*- coding: utf-8 -*-

from odoo import models, fields, api


class PreferenceRankingToken(models.Model):
    _name = "preference.ranking.token"
    _description = "Preference Ranking Token"

    type = fields.Selection(
        [
            ("QC", "QC"),
            ("Eval", "Eval"),
            ("Reeval", "Reeval"),
            ("Prompt Rejection", "Prompt Rejection"),
            ("Prompt Enhancement", "Prompt Enhancement"),
        ],
        string="Type",
    )
    preference_record_ids = fields.Many2many(
        "preference.ranking", string="Preference Records"
    )
    ai_model_type = fields.Selection(
        [("openai", "GPT"), ("gemini", "Gemini"), ("kimi", "Kimi")],
        string=" AI Model Type",
    )
    token_line = fields.One2many(
        "preference.ranking.token.line", "token_id", string="Token Lines"
    )


class PreferenceRankingTokenLine(models.Model):
    _name = "preference.ranking.token.line"
    _description = "Preference Ranking Token Line"

    token_id = fields.Many2one("preference.ranking.token", string="Token")
    input_token = fields.Float(string="Input Token")
    output_token = fields.Float(string="Output Token")
    cache_token = fields.Float(string="Cache Token")
    cost = fields.Float(string="Cost")
