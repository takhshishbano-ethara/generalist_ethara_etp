from __future__ import annotations

import logging

from odoo import api, fields, models

from . import credential_manager

_logger = logging.getLogger(__name__)


_VERTEX_KEY_PARAM = "lynceus.vertex_api_key"
_MASK = "********"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    lynceus_vertex_api_key = fields.Char(
        string="Vertex AI API Key",
        help="Stored encrypted. Once set, the field shows a mask - submit a new value to rotate.",
    )
    lynceus_vertex_api_key_is_set = fields.Boolean(
        string="Vertex AI Key Configured",
        compute="_compute_lynceus_vertex_api_key_is_set",
    )
    lynceus_vertex_model = fields.Char(
        string="Vertex AI Model",
        config_parameter="lynceus.vertex_model",
        default="gemini-3.5-flash",
        help="Gemini model ID as exposed by Vertex AI Express endpoint, "
             "e.g. gemini-3.5-flash.",
    )
    lynceus_vertex_base_url = fields.Char(
        string="Vertex AI Base URL",
        config_parameter="lynceus.vertex_base_url",
        default="https://aiplatform.googleapis.com/v1/publishers/google/models",
        help="Vertex AI publishers endpoint root. Client appends "
             "'/{model}:generateContent?key=...'.",
    )
    lynceus_vertex_max_retries = fields.Integer(
        string="Vertex Max Retries",
        config_parameter="lynceus.vertex_max_retries",
        default=3,
    )

    lynceus_batch_call_size = fields.Integer(
        string="Prompts per LLM Call",
        config_parameter="lynceus.batch_call_size",
        default=20,
        help="How many prompts a single Gemini call returns. Each parsed "
             "prompt becomes one DB record. 20 is a safe default; raise only "
             "after measuring yield and quality.",
    )
    lynceus_vertex_max_output_tokens = fields.Integer(
        string="Max Output Tokens / Call",
        config_parameter="lynceus.vertex_max_output_tokens",
        default=4000,
        help="Output token ceiling for one batched call. ~150 tokens per "
             "prompt is a safe budget (50 words + JSON overhead), so 4000 "
             "comfortably covers 20 prompts.",
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
             "Higher = faster but watch Vertex AI rate limits.",
    )
    lynceus_bulk_insert_chunk = fields.Integer(
        string="Bulk Insert Chunk Size",
        config_parameter="lynceus.bulk_insert_chunk",
        default=50,
        help="Number of generated prompts to insert at once. 50 is the sweet "
             "spot between commit frequency and write throughput.",
    )

    @api.depends("lynceus_vertex_api_key")
    def _compute_lynceus_vertex_api_key_is_set(self):
        ICP = self.env["ir.config_parameter"].sudo()
        for rec in self:
            rec.lynceus_vertex_api_key_is_set = credential_manager.is_set(
                ICP, _VERTEX_KEY_PARAM,
            )

    @api.model
    def get_values(self):
        values = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        values["lynceus_vertex_api_key"] = (
            _MASK if credential_manager.is_set(ICP, _VERTEX_KEY_PARAM) else ""
        )
        return values

    def set_values(self):
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()
        raw_vertex = (self.lynceus_vertex_api_key or "").strip()
        if raw_vertex and raw_vertex != _MASK:
            ICP.set_param(
                _VERTEX_KEY_PARAM,
                credential_manager.encrypt(ICP, raw_vertex),
            )
