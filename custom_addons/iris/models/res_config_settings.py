"""Iris settings — LLM gateway, S3 connector, hold policy, prompt overrides.

Follows the i2i masked-key pattern: the OpenRouter API key is stored
Fernet-encrypted via ``credential_manager`` and the Settings form only ever
shows a mask once a key exists; submitting a new (non-mask) value rotates it.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import credential_manager

_API_KEY_PARAM = "iris.openrouter_api_key"
_S3_CONNECTOR_PARAM = "iris.s3_connector_id"
_MASK = "********"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ------------------------------------------------------------------
    # LLM gateway
    # ------------------------------------------------------------------
    iris_openrouter_api_key = fields.Char(
        string="OpenRouter API Key",
        help="API key for the LLM gateway. Stored Fernet-encrypted; the "
             "form shows a mask once set — submit a new value to rotate.",
    )
    iris_api_key_is_set = fields.Boolean(
        string="API Key Configured",
        compute="_compute_iris_api_key_is_set",
    )
    iris_llm_model = fields.Char(
        string="LLM Model",
        config_parameter="iris.llm_model",
        default="anthropic/claude-sonnet-4.5",
        help="Model slug on the gateway, e.g. anthropic/claude-sonnet-4.5, "
             "moonshotai/kimi-k2, google/gemini-2.5-pro.",
    )
    iris_openrouter_base_url = fields.Char(
        string="LLM Base URL",
        config_parameter="iris.openrouter_base_url",
        default="https://openrouter.ai/api/v1",
        help="Any OpenAI-compatible /chat/completions gateway root.",
    )
    iris_llm_timeout = fields.Integer(
        string="LLM Timeout (s)",
        config_parameter="iris.llm_timeout",
        default=180,
    )
    iris_openrouter_max_retries = fields.Integer(
        string="LLM Max Retries",
        config_parameter="iris.openrouter_max_retries",
        default=3,
    )
    iris_usd_per_mtoken = fields.Float(
        string="Fallback Cost (USD per 1M tokens)",
        config_parameter="iris.usd_per_mtoken",
        default=3.0,
        digits=(12, 6),
        help="Used to approximate cost from token usage when the gateway "
             "does not report a request cost.",
    )

    # ------------------------------------------------------------------
    # Hold policy + storage
    # ------------------------------------------------------------------
    iris_hold_business_days = fields.Integer(
        string="Hold Business Days",
        config_parameter="iris.hold_business_days",
        default=5,
        help="Business days (Mon-Fri) before an unverified HOLD is "
             "auto-closed to BLOCK by the daily cron.",
    )
    iris_s3_connector_id = fields.Many2one(
        "s3.connector",
        string="Resume S3 Connector",
        help="Connector used for private resume storage. When empty, the "
             "first s3.connector record is used; when none exists, resumes "
             "stay local-only (attachment).",
    )

    # ------------------------------------------------------------------
    # v1.1 — Roles, batches, duplicate detection
    # ------------------------------------------------------------------
    iris_enable_role_creation = fields.Boolean(
        string="Enable Role Creation",
        config_parameter="iris.enable_role_creation",
        help="v1.1 ships with the seeded Head of Engineering role only. "
             "Tick to unlock creating additional role profiles (the v1.2 "
             "unlock — one config flip, no code change).",
    )
    iris_batch_max_members = fields.Integer(
        string="Batch Max Members",
        config_parameter="iris.batch_max_members",
        default=10,
        help="Maximum candidates per screening batch (caps the token "
             "budget of the LLM consistency pass).",
    )
    iris_dup_similarity_threshold = fields.Float(
        string="Duplicate Similarity Threshold",
        config_parameter="iris.dup_similarity_threshold",
        default=0.90,
        digits=(3, 2),
        help="Near-duplicate resume detection ratio (0-1). Two resumes "
             "with a similarity at or above this are flagged as possible "
             "duplicates. Advisory only — never blocks an upload.",
    )
    iris_dup_scan_limit = fields.Integer(
        string="Duplicate Scan Limit",
        config_parameter="iris.dup_scan_limit",
        default=200,
        help="How many of the most recent resumes the near-duplicate scan "
             "compares against on each upload.",
    )

    # ------------------------------------------------------------------
    # Prompt overrides (file-first fallback in services/prompt_loader.py).
    # NOTE: res.config.settings forbids Text fields with config_parameter=
    # (only boolean/integer/float/char/selection/many2one/datetime), so the
    # ICP round-trip is done manually in get_values()/set_values() to keep
    # the multiline textarea widget.
    # ------------------------------------------------------------------
    iris_prompt_screening = fields.Text(
        string="Screening Prompt Override",
        help="When set (non-empty), replaces the bundled prompts/SCREENING.md.",
    )
    iris_prompt_questions = fields.Text(
        string="Questions Prompt Override",
        help="When set (non-empty), replaces the bundled prompts/QUESTIONS.md.",
    )
    iris_prompt_scorecard = fields.Text(
        string="Scorecard Prompt Override",
        help="When set (non-empty), replaces the bundled prompts/SCORECARD.md.",
    )
    iris_prompt_batch_consistency = fields.Text(
        string="Batch Consistency Prompt Override",
        help="When set (non-empty), replaces the bundled "
             "prompts/BATCH_CONSISTENCY.md.",
    )
    iris_prompt_jd_critique = fields.Text(
        string="JD Critique Prompt Override",
        help="When set (non-empty), replaces the bundled "
             "prompts/JD_CRITIQUE.md.",
    )
    iris_prompt_jd_rewrite = fields.Text(
        string="JD Rewrite Prompt Override",
        help="When set (non-empty), replaces the bundled "
             "prompts/JD_REWRITE.md.",
    )
    iris_prompt_assessment_review = fields.Text(
        string="Assessment Review Prompt Override",
        help="When set (non-empty), replaces the bundled "
             "prompts/ASSESSMENT_REVIEW.md.",
    )
    iris_prompt_clarifying_questions = fields.Text(
        string="Clarifying Questions Prompt Override",
        help="When set (non-empty), replaces the bundled "
             "prompts/CLARIFYING_QUESTIONS.md.",
    )

    _PROMPT_PARAMS = {
        "iris_prompt_screening": "iris.prompt_screening",
        "iris_prompt_questions": "iris.prompt_questions",
        "iris_prompt_scorecard": "iris.prompt_scorecard",
        "iris_prompt_batch_consistency": "iris.prompt_batch_consistency",
        "iris_prompt_jd_critique": "iris.prompt_jd_critique",
        "iris_prompt_jd_rewrite": "iris.prompt_jd_rewrite",
        "iris_prompt_assessment_review": "iris.prompt_assessment_review",
        "iris_prompt_clarifying_questions": "iris.prompt_clarifying_questions",
    }

    # ------------------------------------------------------------------
    # Computes / get-set round trip
    # ------------------------------------------------------------------
    def _compute_iris_api_key_is_set(self):
        is_set = bool(
            self.env["ir.config_parameter"].sudo().get_param(_API_KEY_PARAM)
        )
        for rec in self:
            rec.iris_api_key_is_set = is_set

    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        existing = ICP.get_param(_API_KEY_PARAM, "")
        res["iris_openrouter_api_key"] = _MASK if existing else ""
        connector_id = ICP.get_param(_S3_CONNECTOR_PARAM, "")
        try:
            res["iris_s3_connector_id"] = int(connector_id) if connector_id else False
        except (TypeError, ValueError):
            res["iris_s3_connector_id"] = False
        for field_name, param in self._PROMPT_PARAMS.items():
            res[field_name] = ICP.get_param(param, "")
        return res

    def set_values(self):
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()
        submitted = (self.iris_openrouter_api_key or "").strip()
        if submitted and submitted != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _API_KEY_PARAM, submitted,
            )
        ICP.set_param(
            _S3_CONNECTOR_PARAM,
            str(self.iris_s3_connector_id.id) if self.iris_s3_connector_id else "",
        )
        for field_name, param in self._PROMPT_PARAMS.items():
            ICP.set_param(param, self[field_name] or "")

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains(
        "iris_llm_timeout", "iris_openrouter_max_retries",
        "iris_usd_per_mtoken", "iris_hold_business_days",
        "iris_batch_max_members", "iris_dup_similarity_threshold",
        "iris_dup_scan_limit",
    )
    def _check_iris_limits(self):
        for rec in self:
            if rec.iris_llm_timeout <= 0:
                raise ValidationError(_("LLM Timeout must be positive."))
            if not (1 <= rec.iris_openrouter_max_retries <= 10):
                raise ValidationError(_(
                    "LLM Max Retries must be between 1 and 10."
                ))
            if rec.iris_usd_per_mtoken < 0:
                raise ValidationError(_(
                    "Fallback cost per 1M tokens must be non-negative."
                ))
            if rec.iris_hold_business_days < 1:
                raise ValidationError(_(
                    "Hold Business Days must be at least 1."
                ))
            if rec.iris_batch_max_members < 2:
                raise ValidationError(_(
                    "Batch Max Members must be at least 2 — a batch "
                    "consistency pass needs at least two candidates."
                ))
            if not (0 < rec.iris_dup_similarity_threshold <= 1):
                raise ValidationError(_(
                    "Duplicate Similarity Threshold must be between 0 "
                    "(exclusive) and 1 (inclusive)."
                ))
            if rec.iris_dup_scan_limit < 0:
                raise ValidationError(_(
                    "Duplicate Scan Limit must be non-negative."
                ))
