import logging

from odoo import api, fields, models

from . import credential_manager

_logger = logging.getLogger(__name__)

_API_KEY_PARAM = "crowley.openrouter_api_key"
_AWS_ACCESS_KEY_PARAM = "crowley.aws_access_key"
_AWS_SECRET_KEY_PARAM = "crowley.aws_secret_key"
_WEBHOOK_SECRET_PARAM = "crowley.webhook_secret"
_S3_CONNECTOR_PARAM = "crowley.s3_connector_id"
_MASK = "********"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ── OpenRouter API Key (encrypted, masked on read) ────────────────────
    crowley_openrouter_api_key = fields.Char(
        string="OpenRouter API Key",
        help="API key for OpenRouter. Stored encrypted; the form shows a "
        "mask once set — submit a new value to rotate.",
    )
    crowley_api_key_is_set = fields.Boolean(
        string="API Key Configured",
        compute="_compute_api_key_is_set",
    )

    # ── AWS credentials (encrypted, masked on read) ───────────────────────
    crowley_aws_access_key = fields.Char(string="AWS Access Key ID")
    crowley_aws_access_key_is_set = fields.Boolean(
        string="AWS Access Key Configured", compute="_compute_aws_access_key_is_set"
    )
    crowley_aws_secret_key = fields.Char(string="AWS Secret Access Key")
    crowley_aws_secret_key_is_set = fields.Boolean(
        string="AWS Secret Configured", compute="_compute_aws_secret_key_is_set"
    )
    crowley_webhook_secret = fields.Char(string="Webhook HMAC Secret")
    crowley_webhook_secret_is_set = fields.Boolean(
        string="Webhook Secret Configured", compute="_compute_webhook_secret_is_set"
    )
    crowley_presigned_ttl_seconds = fields.Integer(
        string="Presigned URL TTL (seconds)",
        config_parameter="crowley.presigned_ttl_seconds",
        default=300,
    )
    crowley_verify_after_upload = fields.Boolean(
        string="Verify SHA-256 after S3 upload",
        config_parameter="crowley.verify_after_upload",
        default=False,
    )
    crowley_webhook_path = fields.Char(
        string="Webhook URL",
        config_parameter="crowley.webhook_path",
        help="Public URL to register with OpenRouter. Auto-suggested from "
             "web.base.url on first open; editable so you can override "
             "(e.g. for an ngrok / Cloudflare Tunnel domain that differs from "
             "Odoo's base URL).",
    )

    # ── OpenRouter request defaults ───────────────────────────────────────
    crowley_default_model = fields.Char(
        string="Default Model",
        config_parameter="crowley.default_model",
        default="bytedance/seedance-2.0",
    )
    crowley_http_referer = fields.Char(
        string="HTTP Referer",
        config_parameter="crowley.http_referer",
        help="Optional HTTP-Referer header sent to OpenRouter (used for "
        "leaderboard attribution).",
    )
    crowley_app_title = fields.Char(
        string="App Title",
        config_parameter="crowley.app_title",
        default="Ethara Crowley",
        help="X-Title header sent to OpenRouter.",
    )

    # ── Generation defaults ───────────────────────────────────────────────
    crowley_default_resolution = fields.Selection(
        selection=[("480p", "480p"), ("720p", "720p"), ("1080p", "1080p")],
        string="Default Resolution",
        config_parameter="crowley.default_resolution",
        default="720p",
    )
    crowley_default_duration = fields.Selection(
        selection=[(str(i), f"{i}s") for i in (4, 5, 6, 7, 8, 9, 10, 12, 15)],
        string="Default Duration",
        config_parameter="crowley.default_duration",
        default="5",
    )

    # ── Polling ───────────────────────────────────────────────────────────
    crowley_poll_interval_seconds = fields.Integer(
        string="Poll Interval (s)",
        config_parameter="crowley.poll_interval_seconds",
        default=15,
    )
    crowley_max_poll_attempts = fields.Integer(
        string="Max Poll Attempts",
        config_parameter="crowley.max_poll_attempts",
        default=80,
    )

    # ── S3 storage ────────────────────────────────────────────────────────
    crowley_s3_prefix = fields.Char(
        string="S3 Key Prefix",
        config_parameter="crowley.s3_prefix",
        default="crowley/",
    )
    crowley_s3_connector_id = fields.Many2one(
        "s3.connector",
        string="S3 Connector",
        help="S3 connector used to upload generated video assets.",
    )

    # ── Computed: whether API key is configured ───────────────────────────
    @api.depends()
    def _compute_api_key_is_set(self):
        for rec in self:
            rec.crowley_api_key_is_set = bool(
                credential_manager.get_encrypted_param(self.env, _API_KEY_PARAM)
            )

    def _compute_aws_access_key_is_set(self):
        for rec in self:
            rec.crowley_aws_access_key_is_set = bool(
                credential_manager.get_encrypted_param(self.env, "crowley.aws_access_key")
            )

    def _compute_aws_secret_key_is_set(self):
        for rec in self:
            rec.crowley_aws_secret_key_is_set = bool(
                credential_manager.get_encrypted_param(self.env, "crowley.aws_secret_key")
            )

    def _compute_webhook_secret_is_set(self):
        for rec in self:
            rec.crowley_webhook_secret_is_set = bool(
                credential_manager.get_encrypted_param(self.env, "crowley.webhook_secret")
            )

    # ── get/set overrides for encrypted API key + Many2one S3 connector ──
    @api.model
    def get_values(self):
        res = super().get_values()
        icp = self.env["ir.config_parameter"].sudo()

        # Webhook URL: seed from web.base.url on first read if unset, so the
        # field shows the sensible default but the user can still override it.
        if not res.get("crowley_webhook_path"):
            base_url = icp.get_param("web.base.url", "")
            res["crowley_webhook_path"] = (
                f"{base_url.rstrip('/')}/crowley/webhook" if base_url else "/crowley/webhook"
            )

        # API key: never expose the actual value; mask if set, empty otherwise.
        existing = credential_manager.get_encrypted_param(self.env, _API_KEY_PARAM)
        res["crowley_openrouter_api_key"] = _MASK if existing else ""

        # AWS access key: mask if set, empty otherwise.
        existing_aws_access = credential_manager.get_encrypted_param(
            self.env, _AWS_ACCESS_KEY_PARAM
        )
        res["crowley_aws_access_key"] = _MASK if existing_aws_access else ""

        # AWS secret key: mask if set, empty otherwise.
        existing_aws_secret = credential_manager.get_encrypted_param(
            self.env, _AWS_SECRET_KEY_PARAM
        )
        res["crowley_aws_secret_key"] = _MASK if existing_aws_secret else ""

        # Webhook secret: mask if set, empty otherwise.
        existing_webhook = credential_manager.get_encrypted_param(
            self.env, _WEBHOOK_SECRET_PARAM
        )
        res["crowley_webhook_secret"] = _MASK if existing_webhook else ""

        # S3 connector: parse stored id, fall back to False if invalid/missing.
        raw = icp.get_param(_S3_CONNECTOR_PARAM, "")
        try:
            sid = int(raw) if raw else False
        except ValueError:
            sid = False
        if sid and self.env["s3.connector"].sudo().browse(sid).exists():
            res["crowley_s3_connector_id"] = sid
        else:
            res["crowley_s3_connector_id"] = False
        return res

    def set_values(self):
        super().set_values()
        icp = self.env["ir.config_parameter"].sudo()

        # API key: only write when user submitted a real new value.
        submitted = (self.crowley_openrouter_api_key or "").strip()
        if submitted and submitted != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _API_KEY_PARAM, submitted,
            )

        # AWS access key: only write when user submitted a real new value.
        submitted_aws_access = (self.crowley_aws_access_key or "").strip()
        if submitted_aws_access and submitted_aws_access != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _AWS_ACCESS_KEY_PARAM, submitted_aws_access,
            )

        # AWS secret key: only write when user submitted a real new value.
        submitted_aws_secret = (self.crowley_aws_secret_key or "").strip()
        if submitted_aws_secret and submitted_aws_secret != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _AWS_SECRET_KEY_PARAM, submitted_aws_secret,
            )

        # Webhook secret: only write when user submitted a real new value.
        submitted_webhook = (self.crowley_webhook_secret or "").strip()
        if submitted_webhook and submitted_webhook != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _WEBHOOK_SECRET_PARAM, submitted_webhook,
            )

        # S3 connector: store the id as a string, empty when cleared.
        sid = self.crowley_s3_connector_id.id if self.crowley_s3_connector_id else ""
        icp.set_param(_S3_CONNECTOR_PARAM, str(sid) if sid else "")
