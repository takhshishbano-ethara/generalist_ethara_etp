import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import credential_manager

_logger = logging.getLogger(__name__)

_API_KEY_PARAM = "t2av.openrouter_api_key"
_AWS_ACCESS_KEY_PARAM = "t2av.aws_access_key"
_AWS_SECRET_KEY_PARAM = "t2av.aws_secret_key"
_BEDROCK_API_KEY_PARAM = "t2av.bedrock_api_key"
_WEBHOOK_SECRET_PARAM = "t2av.webhook_secret"
_S3_CONNECTOR_PARAM = "t2av.s3_connector_id"
_MASK = "********"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ── OpenRouter API Key (encrypted, masked on read) ────────────────────
    t2av_openrouter_api_key = fields.Char(
        string="OpenRouter API Key",
        help="API key for OpenRouter. Stored encrypted; the form shows a "
        "mask once set — submit a new value to rotate.",
    )
    t2av_api_key_is_set = fields.Boolean(
        string="API Key Configured",
        compute="_compute_t2av_api_key_is_set",
    )

    # ── AWS credentials (encrypted, masked on read) ───────────────────────
    t2av_aws_access_key = fields.Char(string="AWS Access Key ID")
    t2av_aws_access_key_is_set = fields.Boolean(
        string="AWS Access Key Configured", compute="_compute_t2av_aws_access_key_is_set"
    )
    t2av_aws_secret_key = fields.Char(string="AWS Secret Access Key")
    t2av_aws_secret_key_is_set = fields.Boolean(
        string="AWS Secret Configured", compute="_compute_t2av_aws_secret_key_is_set"
    )
    t2av_bedrock_api_key = fields.Char(
        string="Bedrock API Key",
        help="AWS Bedrock API Key (starts with ABSK...). "
             "Single-key alternative to AWS Access Key + Secret Key for "
             "Bedrock auth. Preferred when set: no SigV4 signing, direct "
             "HTTPS Bearer auth to bedrock-runtime endpoint.",
    )
    t2av_bedrock_api_key_is_set = fields.Boolean(
        string="Bedrock API Key Configured",
        compute="_compute_t2av_bedrock_api_key_is_set",
    )
    t2av_webhook_secret = fields.Char(string="Webhook HMAC Secret")
    t2av_webhook_secret_is_set = fields.Boolean(
        string="Webhook Secret Configured", compute="_compute_t2av_webhook_secret_is_set"
    )
    t2av_presigned_ttl_seconds = fields.Integer(
        string="Presigned URL TTL (seconds)",
        config_parameter="t2av.presigned_ttl_seconds",
        default=300,
    )
    t2av_verify_after_upload = fields.Boolean(
        string="Verify SHA-256 after S3 upload",
        config_parameter="t2av.verify_after_upload",
        default=False,
    )
    t2av_webhook_path = fields.Char(
        string="Webhook URL",
        config_parameter="t2av.webhook_path",
        help="Public URL to register with OpenRouter. Auto-suggested from "
             "web.base.url on first open; editable so you can override "
             "(e.g. for an ngrok / Cloudflare Tunnel domain that differs from "
             "Odoo's base URL).",
    )

    # ── OpenRouter request defaults ───────────────────────────────────────
    t2av_default_model = fields.Char(
        string="Default Model",
        config_parameter="t2av.default_model",
        default="bytedance/seedance-2.0",
    )
    t2av_http_referer = fields.Char(
        string="HTTP Referer",
        config_parameter="t2av.http_referer",
        help="Optional HTTP-Referer header sent to OpenRouter (used for "
        "leaderboard attribution).",
    )
    t2av_app_title = fields.Char(
        string="App Title",
        config_parameter="t2av.app_title",
        default="Ethara T2AV",
        help="X-Title header sent to OpenRouter.",
    )

    t2av_bedrock_region = fields.Char(
        string="Bedrock AWS Region",
        config_parameter="t2av.bedrock_region",
        default="ap-south-1",
        help="AWS region for Bedrock (enrichment + optional Bedrock-backed review).",
    )
    t2av_bedrock_model_id = fields.Char(
        string="Enrichment Model ID (Bedrock)",
        config_parameter="t2av.bedrock_model_id",
        help="Target: Claude Sonnet 4.6 — paste exact ARN when AWS publishes it.",
    )
    t2av_bedrock_max_retries = fields.Integer(
        string="Bedrock Max Retries",
        config_parameter="t2av.bedrock_max_retries",
        default=5,
    )
    t2av_enrichment_max_attempts = fields.Integer(
        string="Enrichment Max Attempts per Job",
        config_parameter="t2av.enrichment_max_attempts",
        default=3,
    )
    t2av_review_provider = fields.Selection(
        [("openrouter", "OpenRouter (Gemini multimodal)"),
         ("bedrock", "AWS Bedrock (Claude vision frames)")],
        string="Video Review Provider",
        config_parameter="t2av.review_provider",
        default="openrouter",
        help="OpenRouter routes the review to Gemini 3.1 Pro multimodal "
             "(text + video URL in the same turn — judges prompt fidelity "
             "AND actual video frames). Bedrock uses Claude vision with "
             "20 ffmpeg-extracted frames as a fallback.",
    )
    t2av_openrouter_review_model_id = fields.Char(
        string="Review Model ID (OpenRouter)",
        config_parameter="t2av.openrouter_review_model_id",
        default="google/gemini-3.1-pro-preview",
        help="OpenRouter model slug for the review. Default: Gemini 3.1 Pro "
             "Preview (multimodal — accepts video URL inline). Existing live "
             "value is preserved on upgrade; flip here when ready to switch.",
    )
    t2av_review_max_attempts = fields.Integer(
        string="Review Max Attempts per Attempt",
        config_parameter="t2av.review_max_attempts",
        default=3,
        help="How many review rounds an attempt can spend before auto-retry "
             "stops. Mirrors enrichment_max_attempts. Each reject below this "
             "limit auto-queues the next round with previous failures injected.",
    )
    t2av_review_max_parallel = fields.Integer(
        string="Review Max Parallel In-Flight",
        config_parameter="t2av.review_max_parallel",
        default=4,
        help="Cap on concurrent submitting reviews. The cron dispatcher checks "
             "this count and only releases capacity = max_parallel - in_flight.",
    )
    t2av_review_batch_size = fields.Integer(
        string="Review Batch Size per Cron Tick",
        config_parameter="t2av.review_batch_size",
        default=8,
        help="Upper bound on how many queued reviews the cron dispatches "
             "per tick; effective dispatch is min(batch_size, capacity).",
    )
    t2av_review_video_url_ttl_seconds = fields.Integer(
        string="Review Video URL TTL (seconds)",
        config_parameter="t2av.review_video_url_ttl_seconds",
        default=900,
        help="TTL of the presigned S3 URL sent to the reviewer. Must outlive "
             "the LLM round-trip; bump if Gemini reports url-fetch errors.",
    )
    # ── Generation defaults ───────────────────────────────────────────────
    t2av_default_resolution = fields.Selection(
        selection=[("480p", "480p"), ("720p", "720p"), ("1080p", "1080p")],
        string="Default Resolution",
        config_parameter="t2av.default_resolution",
        default="720p",
    )
    t2av_default_duration = fields.Selection(
        selection=[(str(i), f"{i}s") for i in (4, 5, 6, 7, 8, 9, 10, 12, 15)],
        string="Default Duration",
        config_parameter="t2av.default_duration",
        default="5",
    )

    # ── Polling ───────────────────────────────────────────────────────────
    t2av_poll_interval_seconds = fields.Integer(
        string="Poll Interval (s)",
        config_parameter="t2av.poll_interval_seconds",
        default=15,
    )
    t2av_max_poll_attempts = fields.Integer(
        string="Max Poll Attempts",
        config_parameter="t2av.max_poll_attempts",
        default=80,
    )

    # ── S3 storage ────────────────────────────────────────────────────────
    t2av_s3_prefix = fields.Char(
        string="S3 Key Prefix",
        config_parameter="t2av.s3_prefix",
        default="t2av/",
    )
    t2av_s3_connector_id = fields.Many2one(
        "s3.connector",
        string="S3 Connector",
        help="S3 connector used to upload generated video assets.",
    )

    # ── Computed: whether API key is configured ───────────────────────────
    @api.depends()
    def _compute_t2av_api_key_is_set(self):
        for rec in self:
            rec.t2av_api_key_is_set = bool(
                credential_manager.get_encrypted_param(self.env, _API_KEY_PARAM)
            )

    def _compute_t2av_aws_access_key_is_set(self):
        for rec in self:
            rec.t2av_aws_access_key_is_set = bool(
                credential_manager.get_encrypted_param(self.env, "t2av.aws_access_key")
            )

    def _compute_t2av_aws_secret_key_is_set(self):
        for rec in self:
            rec.t2av_aws_secret_key_is_set = bool(
                credential_manager.get_encrypted_param(self.env, "t2av.aws_secret_key")
            )

    def _compute_t2av_bedrock_api_key_is_set(self):
        for rec in self:
            rec.t2av_bedrock_api_key_is_set = bool(
                credential_manager.get_encrypted_param(self.env, _BEDROCK_API_KEY_PARAM)
            )

    def _compute_t2av_webhook_secret_is_set(self):
        for rec in self:
            rec.t2av_webhook_secret_is_set = bool(
                credential_manager.get_encrypted_param(self.env, "t2av.webhook_secret")
            )

    # ── get/set overrides for encrypted API key + Many2one S3 connector ──
    @api.model
    def get_values(self):
        res = super().get_values()
        icp = self.env["ir.config_parameter"].sudo()

        # Webhook URL: seed from web.base.url on first read if unset, so the
        # field shows the sensible default but the user can still override it.
        if not res.get("t2av_webhook_path"):
            base_url = icp.get_param("web.base.url", "")
            res["t2av_webhook_path"] = (
                f"{base_url.rstrip('/')}/t2av/webhook" if base_url else "/t2av/webhook"
            )

        # API key: never expose the actual value; mask if set, empty otherwise.
        existing = credential_manager.get_encrypted_param(self.env, _API_KEY_PARAM)
        res["t2av_openrouter_api_key"] = _MASK if existing else ""

        # AWS access key: mask if set, empty otherwise.
        existing_aws_access = credential_manager.get_encrypted_param(
            self.env, _AWS_ACCESS_KEY_PARAM
        )
        res["t2av_aws_access_key"] = _MASK if existing_aws_access else ""

        # AWS secret key: mask if set, empty otherwise.
        existing_aws_secret = credential_manager.get_encrypted_param(
            self.env, _AWS_SECRET_KEY_PARAM
        )
        res["t2av_aws_secret_key"] = _MASK if existing_aws_secret else ""

        # Bedrock API key: mask if set, empty otherwise.
        existing_bedrock_api = credential_manager.get_encrypted_param(
            self.env, _BEDROCK_API_KEY_PARAM
        )
        res["t2av_bedrock_api_key"] = _MASK if existing_bedrock_api else ""

        # Webhook secret: mask if set, empty otherwise.
        existing_webhook = credential_manager.get_encrypted_param(
            self.env, _WEBHOOK_SECRET_PARAM
        )
        res["t2av_webhook_secret"] = _MASK if existing_webhook else ""

        # S3 connector: parse stored id, fall back to False if invalid/missing.
        raw = icp.get_param(_S3_CONNECTOR_PARAM, "")
        try:
            sid = int(raw) if raw else False
        except ValueError:
            sid = False
        if sid and self.env["s3.connector"].sudo().browse(sid).exists():
            res["t2av_s3_connector_id"] = sid
        else:
            res["t2av_s3_connector_id"] = False
        return res

    def set_values(self):
        super().set_values()
        icp = self.env["ir.config_parameter"].sudo()

        # API key: only write when user submitted a real new value.
        submitted = (self.t2av_openrouter_api_key or "").strip()
        if submitted and submitted != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _API_KEY_PARAM, submitted,
            )

        # AWS access key: only write when user submitted a real new value.
        submitted_aws_access = (self.t2av_aws_access_key or "").strip()
        if submitted_aws_access and submitted_aws_access != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _AWS_ACCESS_KEY_PARAM, submitted_aws_access,
            )

        # AWS secret key: only write when user submitted a real new value.
        submitted_aws_secret = (self.t2av_aws_secret_key or "").strip()
        if submitted_aws_secret and submitted_aws_secret != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _AWS_SECRET_KEY_PARAM, submitted_aws_secret,
            )

        submitted_bedrock_api = (self.t2av_bedrock_api_key or "").strip()
        if submitted_bedrock_api and submitted_bedrock_api != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _BEDROCK_API_KEY_PARAM, submitted_bedrock_api,
            )

        # Webhook secret: only write when user submitted a real new value.
        submitted_webhook = (self.t2av_webhook_secret or "").strip()
        if submitted_webhook and submitted_webhook != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _WEBHOOK_SECRET_PARAM, submitted_webhook,
            )

        # S3 connector: store the id as a string, empty when cleared.
        sid = self.t2av_s3_connector_id.id if self.t2av_s3_connector_id else ""
        icp.set_param(_S3_CONNECTOR_PARAM, str(sid) if sid else "")

    @api.constrains(
        "t2av_bedrock_max_retries",
        "t2av_enrichment_max_attempts",
        "t2av_review_max_attempts",
        "t2av_review_max_parallel",
        "t2av_review_batch_size",
        "t2av_review_video_url_ttl_seconds",
    )
    def _check_bedrock_limits(self):
        for rec in self:
            if not (1 <= rec.t2av_bedrock_max_retries <= 10):
                raise ValidationError(_(
                    "Bedrock Max Retries must be between 1 and 10."
                ))
            if not (1 <= rec.t2av_enrichment_max_attempts <= 5):
                raise ValidationError(_(
                    "Enrichment Max Attempts must be between 1 and 5."
                ))
            if not (1 <= rec.t2av_review_max_attempts <= 5):
                raise ValidationError(_(
                    "Review Max Attempts must be between 1 and 5."
                ))
            if not (1 <= rec.t2av_review_max_parallel <= 32):
                raise ValidationError(_(
                    "Review Max Parallel must be between 1 and 32."
                ))
            if not (1 <= rec.t2av_review_batch_size <= 64):
                raise ValidationError(_(
                    "Review Batch Size must be between 1 and 64."
                ))
            if not (60 <= rec.t2av_review_video_url_ttl_seconds <= 86400):
                raise ValidationError(_(
                    "Review Video URL TTL must be between 60 seconds and 24 hours (86400)."
                ))
