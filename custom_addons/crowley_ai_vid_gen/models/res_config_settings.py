# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # OpenRouter
    crowley_openrouter_api_key = fields.Char(
        string="OpenRouter API Key",
        config_parameter="crowley_ai_vid_gen.openrouter_api_key",
    )
    crowley_openrouter_http_referer = fields.Char(
        string="HTTP-Referer (App Attribution)",
        config_parameter="crowley_ai_vid_gen.openrouter_http_referer",
    )
    crowley_openrouter_app_title = fields.Char(
        string="X-OpenRouter-Title (App Name)",
        config_parameter="crowley_ai_vid_gen.openrouter_app_title",
        default="Crowley AI Vid Gen",
    )
    crowley_openrouter_callback_url = fields.Char(
        string="Webhook Callback URL",
        config_parameter="crowley_ai_vid_gen.openrouter_callback_url",
        help="OpenRouter will POST job completion events here. Leave blank to use cron polling only. "
             "Typical value: https://your-odoo.example.com/crowley/seedance/webhook",
    )
    crowley_webhook_secret = fields.Char(
        string="Webhook Shared Secret",
        config_parameter="crowley_ai_vid_gen.webhook_secret",
        help="HMAC-SHA256 secret shared with OpenRouter. Required if webhook URL is set.",
    )
    crowley_webhook_signature_tolerance = fields.Integer(
        string="Webhook Signature Tolerance (s)",
        config_parameter="crowley_ai_vid_gen.webhook_signature_tolerance",
        default=300,
    )
    crowley_system_prompt = fields.Text(
        string="System Prompt (continuity directive)",
        help=(
            "Hidden prompt prefix prepended to every generation. Tells Seedance "
            "to render a single continuous shot for the full requested duration "
            "without scene cuts. Edit to tune the cinematic-continuity behaviour. "
            "Leave blank to send the user's prompt verbatim."
        ),
    )

    # S3
    crowley_s3_bucket = fields.Char(
        string="S3 Bucket",
        config_parameter="crowley_ai_vid_gen.s3_bucket",
    )
    crowley_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="crowley_ai_vid_gen.s3_region",
        default="us-east-1",
    )
    crowley_s3_access_key = fields.Char(
        string="S3 Access Key",
        config_parameter="crowley_ai_vid_gen.s3_access_key",
        help="Leave empty to use the default AWS credentials chain (env vars / IAM role).",
    )
    crowley_s3_secret_key = fields.Char(
        string="S3 Secret Key",
        config_parameter="crowley_ai_vid_gen.s3_secret_key",
        help=(
            "AWS secret access key. The field appears blank on every reload "
            "(standard Odoo behaviour for password fields). The previously "
            "saved value is preserved automatically — only retype it if you "
            "want to rotate the secret."
        ),
    )
    crowley_s3_endpoint_url = fields.Char(
        string="S3 Endpoint URL Override",
        config_parameter="crowley_ai_vid_gen.s3_endpoint_url",
        help="Leave empty for AWS. Set for MinIO / R2 / Wasabi (e.g. http://localhost:9000).",
    )
    crowley_s3_key_prefix = fields.Char(
        string="S3 Key Prefix",
        config_parameter="crowley_ai_vid_gen.s3_key_prefix",
        default="crowley-seedance/",
    )
    crowley_presigned_ttl_seconds = fields.Integer(
        string="Presigned URL TTL (seconds)",
        config_parameter="crowley_ai_vid_gen.presigned_ttl_seconds",
        default=300,
    )
    crowley_verify_after_upload = fields.Boolean(
        string="Verify SHA-256 after S3 upload",
        config_parameter="crowley_ai_vid_gen.verify_after_upload",
        help="Re-fetch from S3 and recompute SHA-256 to prove byte-identity. Doubles S3 egress; off by default.",
    )

    # Polling
    crowley_poll_interval_seconds = fields.Integer(
        string="Poll Interval (seconds)",
        config_parameter="crowley_ai_vid_gen.poll_interval_seconds",
        default=60,
    )
    crowley_max_poll_attempts = fields.Integer(
        string="Max Poll Attempts",
        config_parameter="crowley_ai_vid_gen.max_poll_attempts",
        default=60,
    )

    # ``password="True"`` Char fields always render blank in Odoo's settings
    # UI even when a value is stored, so saving the form would otherwise erase
    # secrets that the admin did not retype. We intercept ``set_values`` and
    # preserve the existing ``ir.config_parameter`` value for any secret
    # field that was submitted empty.
    _SECRET_FIELDS = (
        ("crowley_openrouter_api_key", "crowley_ai_vid_gen.openrouter_api_key"),
        ("crowley_webhook_secret", "crowley_ai_vid_gen.webhook_secret"),
        ("crowley_s3_access_key", "crowley_ai_vid_gen.s3_access_key"),
        ("crowley_s3_secret_key", "crowley_ai_vid_gen.s3_secret_key"),
    )

    def set_values(self):
        ICP = self.env["ir.config_parameter"].sudo()
        for field_name, param_key in self._SECRET_FIELDS:
            submitted = self[field_name] if field_name in self._fields else None
            if not submitted:
                existing = ICP.get_param(param_key)
                if existing:
                    self[field_name] = existing
        super().set_values()
        ICP.set_param("crowley_ai_vid_gen.system_prompt", self.crowley_system_prompt or "")
        self.env["crowley.ai.vid.gen.s3.storage"].clear_cache()

    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        res["crowley_system_prompt"] = ICP.get_param("crowley_ai_vid_gen.system_prompt", "")
        return res
