from __future__ import annotations

import logging

from odoo import api, fields, models

from . import credential_manager

_logger = logging.getLogger(__name__)


_ANTHROPIC_KEY_PARAM = "lynceus.anthropic_api_key"
_OPENROUTER_KEY_PARAM = "lynceus.openrouter_api_key"
_MASK = "********"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    lynceus_provider = fields.Selection(
        selection=[
            ("anthropic", "Anthropic (Claude)"),
            ("openrouter", "OpenRouter (Gemini / multi-model)"),
        ],
        string="Active LLM Provider",
        config_parameter="lynceus.provider",
        default="anthropic",
        help="Selects which LLM is used for batch generation. Both keys can be "
             "configured simultaneously; only the active provider is called.",
    )

    lynceus_anthropic_api_key = fields.Char(
        string="Anthropic API Key",
        help="Stored encrypted. Once set, the form shows a mask - submit a new value to rotate.",
    )
    lynceus_anthropic_api_key_is_set = fields.Boolean(
        string="Anthropic Key Configured",
        compute="_compute_lynceus_anthropic_api_key_is_set",
    )
    lynceus_anthropic_model = fields.Char(
        string="Anthropic Model",
        config_parameter="lynceus.anthropic_model",
        default="claude-sonnet-4-6",
    )
    lynceus_anthropic_base_url = fields.Char(
        string="Anthropic Base URL",
        config_parameter="lynceus.anthropic_base_url",
        default="https://api.anthropic.com/v1/messages",
    )
    lynceus_anthropic_version = fields.Char(
        string="Anthropic API Version",
        config_parameter="lynceus.anthropic_version",
        default="2023-06-01",
    )

    lynceus_openrouter_api_key = fields.Char(
        string="OpenRouter API Key",
        help="Stored encrypted. Once set, the form shows a mask - submit a new value to rotate.",
    )
    lynceus_openrouter_api_key_is_set = fields.Boolean(
        string="OpenRouter Key Configured",
        compute="_compute_lynceus_openrouter_api_key_is_set",
    )
    lynceus_openrouter_model = fields.Char(
        string="OpenRouter Model Slug",
        config_parameter="lynceus.openrouter_model",
        default="google/gemini-3.5-flash",
        help="Any OpenRouter model slug, e.g. google/gemini-3.5-flash, "
             "anthropic/claude-3.5-sonnet, openai/gpt-4o-mini.",
    )
    lynceus_openrouter_base_url = fields.Char(
        string="OpenRouter Base URL",
        config_parameter="lynceus.openrouter_base_url",
        default="https://openrouter.ai/api/v1",
        help="OpenRouter API root. Client appends '/chat/completions'.",
    )
    lynceus_openrouter_app_title = fields.Char(
        string="OpenRouter App Title",
        config_parameter="lynceus.openrouter_app_title",
        default="Ethara Lynceus",
        help="Sent as X-Title header; visible in OpenRouter dashboard.",
    )
    lynceus_openrouter_http_referer = fields.Char(
        string="OpenRouter HTTP Referer",
        config_parameter="lynceus.openrouter_http_referer",
        help="Optional. Sent as HTTP-Referer header.",
    )
    lynceus_openrouter_max_retries = fields.Integer(
        string="OpenRouter Max Retries",
        config_parameter="lynceus.openrouter_max_retries",
        default=3,
    )

    lynceus_max_tokens_per_call = fields.Integer(
        string="Max Tokens per Call",
        config_parameter="lynceus.max_tokens_per_call",
        default=300,
        help="Each prompt is short (35-50 words). 300 leaves slack for the model's variance.",
    )
    lynceus_default_batch_size = fields.Integer(
        string="Default Batch Target N",
        config_parameter="lynceus.default_batch_size",
        default=3000,
    )
    lynceus_default_tasker_quota = fields.Integer(
        string="Default Per-Tasker Daily Quota",
        config_parameter="lynceus.default_tasker_quota",
        default=20,
    )
    lynceus_reclaim_hours = fields.Integer(
        string="Reclaim Window (hours)",
        config_parameter="lynceus.reclaim_hours",
        default=24,
    )
    lynceus_pool_depletion_threshold = fields.Integer(
        string="Pool Depletion Alert Threshold",
        config_parameter="lynceus.pool_depletion_threshold",
        default=500,
        help="When AVAILABLE prompts drop below this, the depletion-alert cron logs a warning.",
    )
    lynceus_parallel_calls = fields.Integer(
        string="Parallel API Calls",
        config_parameter="lynceus.parallel_calls",
        default=10,
        help="Max concurrent HTTP requests during batch generation. "
             "Higher = faster but watch provider rate limits. "
             "Recommended: 10 for OpenRouter Gemini Flash, 5-8 for Anthropic Claude paid tier.",
    )
    lynceus_bulk_insert_chunk = fields.Integer(
        string="Bulk Insert Chunk Size",
        config_parameter="lynceus.bulk_insert_chunk",
        default=50,
        help="Number of generated prompts to insert at once. 50 is the sweet spot "
             "between commit frequency and write throughput.",
    )

    @api.depends("lynceus_anthropic_api_key")
    def _compute_lynceus_anthropic_api_key_is_set(self):
        ICP = self.env["ir.config_parameter"].sudo()
        for rec in self:
            rec.lynceus_anthropic_api_key_is_set = credential_manager.is_set(ICP, _ANTHROPIC_KEY_PARAM)

    @api.depends("lynceus_openrouter_api_key")
    def _compute_lynceus_openrouter_api_key_is_set(self):
        ICP = self.env["ir.config_parameter"].sudo()
        for rec in self:
            rec.lynceus_openrouter_api_key_is_set = credential_manager.is_set(ICP, _OPENROUTER_KEY_PARAM)

    @api.model
    def get_values(self):
        values = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        values["lynceus_anthropic_api_key"] = _MASK if credential_manager.is_set(ICP, _ANTHROPIC_KEY_PARAM) else ""
        values["lynceus_openrouter_api_key"] = _MASK if credential_manager.is_set(ICP, _OPENROUTER_KEY_PARAM) else ""
        return values

    def set_values(self):
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()
        raw_anthropic = (self.lynceus_anthropic_api_key or "").strip()
        if raw_anthropic and raw_anthropic != _MASK:
            ICP.set_param(_ANTHROPIC_KEY_PARAM, credential_manager.encrypt(ICP, raw_anthropic))
        raw_openrouter = (self.lynceus_openrouter_api_key or "").strip()
        if raw_openrouter and raw_openrouter != _MASK:
            ICP.set_param(_OPENROUTER_KEY_PARAM, credential_manager.encrypt(ICP, raw_openrouter))
