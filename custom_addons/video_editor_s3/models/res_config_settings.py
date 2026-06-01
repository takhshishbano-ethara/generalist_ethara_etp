# -*- coding: utf-8 -*-
import base64
import binascii

from odoo import _, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import s3_storage, youtube_downloader

_LLM_SEED_FILE_PARAM = "video_editor_s3.llm_qc_seed_file"
_LLM_SEED_FILENAME_PARAM = "video_editor_s3.llm_qc_seed_filename"
_LLM_SEED_FILE_MAX_BYTES = 100 * 1024
_LLM_SEED_ALLOWED_EXTS = (".md", ".txt")

_YT_COOKIES_FILE_PARAM = "video_editor_s3.yt_cookies_file"
_YT_COOKIES_FILENAME_PARAM = "video_editor_s3.yt_cookies_filename"
_YT_COOKIES_FILE_MAX_BYTES = 1024 * 1024
_YT_COOKIES_NETSCAPE_HEADERS = (
    "# Netscape HTTP Cookie File",
    "# HTTP Cookie File",
)


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
        string="AWS Access Key (Lambda)",
        config_parameter="video_editor_s3.aws_access_key",
        help="Identity used to invoke the Lambda render pipeline. Not used for direct S3 uploads.",
    )
    video_editor_s3_aws_secret_key = fields.Char(
        string="AWS Secret Key (Lambda)",
        config_parameter="video_editor_s3.aws_secret_key",
    )
    video_editor_s3_s3_access_key = fields.Char(
        string="S3 Access Key (Odoo)",
        config_parameter="video_editor_s3.s3_access_key",
        help=(
            "Identity used by Odoo for direct S3 operations (YouTube ingest upload, "
            "render output upload, dedup HeadObject). Must have s3:PutObject, "
            "s3:GetObject, s3:ListBucket on the bucket. If empty, falls back to "
            "the Lambda credentials above."
        ),
    )
    video_editor_s3_s3_secret_key = fields.Char(
        string="S3 Secret Key (Odoo)",
        config_parameter="video_editor_s3.s3_secret_key",
    )
    video_editor_s3_export_prefix = fields.Char(
        string="Export Key Prefix",
        default="video_editor_s3/exports",
        config_parameter="video_editor_s3.export_prefix",
    )
    video_editor_s3_youtube_prefix = fields.Char(
        string="YouTube Key Prefix",
        default="video_editor_s3",
        config_parameter="video_editor_s3.youtube_prefix",
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
    video_editor_s3_trim_min_seconds = fields.Float(
        string="Trim Min Duration (s)",
        default=8.0,
        config_parameter="video_editor_s3.trim_min_seconds",
        help="Minimum allowed duration of the exported trim video, in seconds.",
    )
    video_editor_s3_trim_max_seconds = fields.Float(
        string="Trim Max Duration (s)",
        default=16.0,
        config_parameter="video_editor_s3.trim_max_seconds",
        help="Maximum allowed duration of the exported trim video, in seconds.",
    )
    video_editor_s3_prompt_max_words = fields.Integer(
        string="Prompt Max Words",
        default=150,
        config_parameter="video_editor_s3.prompt_max_words",
        help="Maximum allowed number of words in the project prompt.",
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
    video_editor_s3_openrouter_api_key = fields.Char(
        string="OpenRouter API Key",
        config_parameter="video_editor_s3.openrouter_api_key",
        help=(
            "OpenRouter API key (sk-or-...). Used by the LLM QC reviewer to "
            "call multimodal models like google/gemini-3.1-pro-preview "
            "through LiteLLM."
        ),
    )
    video_editor_s3_llm_qc_model_id = fields.Char(
        string="LLM QC Model ID",
        default="openrouter/google/gemini-3.1-pro-preview",
        config_parameter="video_editor_s3.llm_qc_model_id",
        help=(
            "Fully-qualified OpenRouter model id used for T2AV row QC. "
            "Default 'openrouter/google/gemini-3.1-pro-preview'. Any "
            "LiteLLM-compatible multimodal model that accepts video_url "
            "content parts will work."
        ),
    )
    video_editor_s3_llm_qc_seed_file = fields.Binary(
        string="LLM QC Seed Prompt File",
        help=(
            "Upload a .md or .txt file (UTF-8, max 100 KB) overriding the "
            "bundled LLM QC reviewer seed at data/llm_qc_seed.md. Clear "
            "the file to revert to the bundled default."
        ),
    )
    video_editor_s3_llm_qc_seed_filename = fields.Char(
        string="LLM QC Seed Prompt Filename",
        config_parameter="video_editor_s3.llm_qc_seed_filename",
    )
    video_editor_s3_yt_cookies_browser = fields.Char(
        string="YouTube Cookies From Browser",
        config_parameter="video_editor_s3.yt_cookies_browser",
        help=(
            "Browser to auto-import YouTube cookies from "
            "(chrome, firefox, edge, brave, safari, opera, vivaldi, chromium, whale). "
            "Optionally append :PROFILE - e.g. 'chrome:Profile 1'. "
            "The browser must be installed on the Odoo host and signed in to YouTube. "
            "Recommended for local dev; for server deployments use the Cookies File Path instead."
        ),
    )
    video_editor_s3_yt_cookies_path = fields.Char(
        string="YouTube Cookies File Path",
        config_parameter="video_editor_s3.yt_cookies_path",
        help=(
            "Absolute path to a Netscape-format cookies.txt exported from a logged-in "
            "YouTube session. Suitable for server deployments. Leave empty to skip. "
            "Ignored when 'YouTube Cookies File' upload is set."
        ),
    )
    video_editor_s3_yt_cookies_file = fields.Binary(
        string="YouTube Cookies File",
        help=(
            "Upload a Netscape-format cookies.txt exported from a logged-in YouTube "
            "session (UTF-8, max 1 MB, first line must start with "
            "'# Netscape HTTP Cookie File' or '# HTTP Cookie File'). When set, this "
            "overrides 'YouTube Cookies File Path' - the file is materialised to a "
            "temporary location per ingest job and removed when the job ends. "
            "Clear the file to revert to the path-based or no-cookies configuration."
        ),
    )
    video_editor_s3_yt_cookies_filename = fields.Char(
        string="YouTube Cookies Filename",
        config_parameter="video_editor_s3.yt_cookies_filename",
    )
    video_editor_s3_yt_proxy_url = fields.Char(
        string="YouTube Proxy URL",
        config_parameter="video_editor_s3.yt_proxy_url",
        help=(
            "Optional HTTP / HTTPS / SOCKS5 proxy used only for YouTube downloads, "
            "e.g. http://user:pass@host:8080 or socks5://host:1080."
        ),
    )

    video_editor_s3_use_lambda = fields.Boolean(
        string="Run heavy jobs on AWS Lambda",
        config_parameter="video_editor_s3.use_lambda",
        help="When enabled, render jobs are dispatched to AWS Lambda instead of the local Odoo worker. YouTube ingest always runs locally on Odoo.",
    )
    video_editor_s3_lambda_function_name = fields.Char(
        string="Lambda Function Name",
        config_parameter="video_editor_s3.lambda_function_name",
        help="e.g. video-pipeline-dev - must match the SAM-deployed function name.",
    )
    video_editor_s3_lambda_region = fields.Char(
        string="Lambda Region",
        config_parameter="video_editor_s3.lambda_region",
        help="AWS region where the Lambda function lives, e.g. ap-south-1.",
    )
    video_editor_s3_lambda_callback_base_url = fields.Char(
        string="Lambda Callback Base URL",
        config_parameter="video_editor_s3.lambda_callback_base_url",
        help="Public Odoo base URL that Lambda will POST callbacks to.",
    )
    video_editor_s3_lambda_webhook_token = fields.Char(
        string="Lambda Webhook HMAC Token",
        config_parameter="video_editor_s3.lambda_webhook_token",
        help="The same secret value stored in AWS Secrets Manager under WebhookTokenSecretArn.",
    )

    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        b64 = ICP.get_param(_LLM_SEED_FILE_PARAM) or ""
        res["video_editor_s3_llm_qc_seed_file"] = b64.encode("ascii") if b64 else False
        yt_b64 = ICP.get_param(_YT_COOKIES_FILE_PARAM) or ""
        res["video_editor_s3_yt_cookies_file"] = yt_b64.encode("ascii") if yt_b64 else False
        return res

    def set_values(self):
        self._validate_llm_qc_seed_file()
        self._validate_youtube_cookies_file()
        ICP = self.env["ir.config_parameter"].sudo()
        raw = self.video_editor_s3_llm_qc_seed_file
        if raw:
            b64 = raw.decode("ascii") if isinstance(raw, bytes) else raw
            ICP.set_param(_LLM_SEED_FILE_PARAM, b64)
        else:
            ICP.set_param(_LLM_SEED_FILE_PARAM, "")
        yt_raw = self.video_editor_s3_yt_cookies_file
        if yt_raw:
            yt_b64 = yt_raw.decode("ascii") if isinstance(yt_raw, bytes) else yt_raw
            ICP.set_param(_YT_COOKIES_FILE_PARAM, yt_b64)
        else:
            ICP.set_param(_YT_COOKIES_FILE_PARAM, "")
        return super().set_values()

    def action_download_llm_qc_seed(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "/video_editor_s3/llm_qc_seed/download",
            "target": "self",
        }

    def _validate_llm_qc_seed_file(self):
        raw_b64 = self.video_editor_s3_llm_qc_seed_file
        if not raw_b64:
            return
        try:
            content = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(_(
                "LLM QC seed file is corrupted (base64 decode failed): %s"
            ) % exc) from exc
        if len(content) > _LLM_SEED_FILE_MAX_BYTES:
            raise ValidationError(_(
                "LLM QC seed file is too large (max %d KB, got %d KB)."
            ) % (_LLM_SEED_FILE_MAX_BYTES // 1024, len(content) // 1024 + 1))
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(_(
                "LLM QC seed file must be valid UTF-8 text: %s"
            ) % exc) from exc
        fn = (self.video_editor_s3_llm_qc_seed_filename or "").lower()
        if fn and not fn.endswith(_LLM_SEED_ALLOWED_EXTS):
            raise ValidationError(_(
                "LLM QC seed file must be .md or .txt (got %s)."
            ) % fn)

    def _validate_youtube_cookies_file(self):
        raw_b64 = self.video_editor_s3_yt_cookies_file
        if not raw_b64:
            return
        try:
            content = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(_(
                "YouTube cookies file is corrupted (base64 decode failed): %s"
            ) % exc) from exc
        if len(content) > _YT_COOKIES_FILE_MAX_BYTES:
            raise ValidationError(_(
                "YouTube cookies file is too large (max %d KB, got %d KB)."
            ) % (_YT_COOKIES_FILE_MAX_BYTES // 1024, len(content) // 1024 + 1))
        if not content.strip():
            raise ValidationError(_("YouTube cookies file is empty."))
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(_(
                "YouTube cookies file must be valid UTF-8 text: %s"
            ) % exc) from exc
        first_line = text.lstrip().splitlines()[0] if text.strip() else ""
        if not any(first_line.startswith(h) for h in _YT_COOKIES_NETSCAPE_HEADERS):
            raise ValidationError(_(
                "YouTube cookies file is not in Netscape format. First line must "
                "start with '# Netscape HTTP Cookie File' or '# HTTP Cookie File'. "
                "Got: %s"
            ) % (first_line[:120] or "(empty)"))

    def action_test_s3_connection(self):
        self.ensure_one()
        cfg = self.env["video.editor.s3.settings"].get_local_s3_config()
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

    def action_test_youtube_cookies(self):
        self.ensure_one()
        cfg = self.env["video.editor.s3.settings"].get_youtube_ingest_config()
        cookies_path = (cfg.get("cookies_path") or "").strip()
        cookies_blob = (cfg.get("cookies_blob_b64") or "").strip()
        proxy_url = (cfg.get("proxy_url") or "").strip()
        if not cookies_path and not cookies_blob and not proxy_url:
            raise UserError(_(
                "Nothing to test - upload a Cookies File, set Cookies File Path, or set Proxy URL first."
            ))
        if cookies_blob:
            self._validate_youtube_cookies_file()
        elif cookies_path:
            youtube_downloader.validate_cookies_file(cookies_path)
        if proxy_url:
            youtube_downloader.validate_proxy_url(proxy_url)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("YouTube ingest configuration OK"),
                "message": _("Cookies (upload/path) and/or proxy URL passed validation."),
                "sticky": False,
            },
        }
