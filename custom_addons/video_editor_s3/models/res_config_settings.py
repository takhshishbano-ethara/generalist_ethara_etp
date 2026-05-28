# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError

from ..services import s3_storage


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    video_editor_s3_aws_bucket = fields.Char(
        string="S3 Bucket",
        config_parameter="video_editor_s3.aws_bucket",
    )
    video_editor_s3_aws_region = fields.Char(
        string="AWS Region",
        default="ap-south-1",
        config_parameter="video_editor_s3.aws_region",
    )
    video_editor_s3_aws_access_key = fields.Char(
        string="AWS Access Key",
        config_parameter="video_editor_s3.aws_access_key",
    )
    video_editor_s3_aws_secret_key = fields.Char(
        string="AWS Secret Key",
        config_parameter="video_editor_s3.aws_secret_key",
    )
    video_editor_s3_export_prefix = fields.Char(
        string="Export Key Prefix",
        default="video_editor_s3/exports",
        config_parameter="video_editor_s3.export_prefix",
    )
    video_editor_s3_youtube_prefix = fields.Char(
        string="YouTube Key Prefix",
        default="video_editor_s3/youtube",
        config_parameter="video_editor_s3.youtube_prefix",
    )
    video_editor_s3_youtube_cookies_file = fields.Char(
        string="YouTube Cookies File Path",
        config_parameter="video_editor_s3.youtube_cookies_file",
        help=(
            "Absolute path to a Netscape-format cookies.txt exported from a "
            "logged-in YouTube session. Recommended for server deployments "
            "when YouTube blocks the host with a bot challenge."
        ),
    )
    video_editor_s3_youtube_cookies_from_browser = fields.Char(
        string="YouTube Cookies From Browser",
        config_parameter="video_editor_s3.youtube_cookies_from_browser",
        help=(
            "Browser name to read cookies from (e.g. chrome, firefox, edge). "
            "The browser must be installed on the Odoo host, signed in to "
            "YouTube, and not currently running. Ignored if 'Cookies File "
            "Path' is set."
        ),
    )
    video_editor_s3_youtube_proxy = fields.Char(
        string="YouTube Proxy URL",
        config_parameter="video_editor_s3.youtube_proxy",
        help=(
            "Optional proxy for yt-dlp (e.g. http://user:pass@host:port or "
            "socks5://host:port). Use a residential proxy if your server IP "
            "is consistently blocked by YouTube."
        ),
    )
    video_editor_s3_max_source_size_mb = fields.Integer(
        string="Max Source Size (MB)",
        default=5120,
        config_parameter="video_editor_s3.max_source_size_mb",
    )
    video_editor_s3_max_concurrent_jobs = fields.Integer(
        string="Max Concurrent Jobs",
        default=2,
        config_parameter="video_editor_s3.max_concurrent_jobs",
    )
    video_editor_s3_ffmpeg_path = fields.Char(
        string="FFmpeg Binary",
        config_parameter="video_editor_s3.ffmpeg_path",
    )
    video_editor_s3_ffprobe_path = fields.Char(
        string="FFprobe Binary",
        config_parameter="video_editor_s3.ffprobe_path",
    )
    video_editor_s3_media_root = fields.Char(
        string="Media Root (server-local)",
        config_parameter="video_editor_s3.media_root",
    )
    video_editor_s3_bedrock_region = fields.Char(
        string="Bedrock Region",
        default="ap-south-1",
        config_parameter="video_editor_s3.bedrock_region",
    )
    video_editor_s3_bedrock_model_id = fields.Char(
        string="Bedrock Model ID",
        default="anthropic.claude-3-5-sonnet-20241022-v2:0",
        config_parameter="video_editor_s3.bedrock_model_id",
    )
    video_editor_s3_bedrock_access_key = fields.Char(
        string="Bedrock Access Key",
        config_parameter="video_editor_s3.bedrock_access_key",
    )
    video_editor_s3_bedrock_secret_key = fields.Char(
        string="Bedrock Secret Key",
        config_parameter="video_editor_s3.bedrock_secret_key",
    )
    video_editor_s3_qc_seed_prompt = fields.Char(
        string="QC Seed Prompt",
        config_parameter="video_editor_s3.qc_seed_prompt",
        help="Seed prompt that defines how prompt QC is performed. Leave empty to use the bundled default.",
    )

    def action_test_s3_connection(self):
        self.ensure_one()
        cfg = self.env["video.editor.s3.settings"].get_s3_config()
        if not cfg.get("bucket"):
            raise UserError(_("Bucket name is required."))
        try:
            s3_storage.validate_credentials(cfg)
        except Exception as exc:
            raise UserError(_("S3 check failed: %s") % exc) from exc
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("S3 connection OK"),
                "message": _("Bucket %s is reachable.") % cfg["bucket"],
                "sticky": False,
            },
        }
