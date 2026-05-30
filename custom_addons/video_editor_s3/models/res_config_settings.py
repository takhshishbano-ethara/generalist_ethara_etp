# -*- coding: utf-8 -*-
import base64
import binascii

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import s3_storage, youtube_downloader

_QC_SEED_FILE_PARAM = "video_editor_s3.qc_seed_file"
_QC_SEED_FILENAME_PARAM = "video_editor_s3.qc_seed_filename"
_QC_SEED_FILE_MAX_BYTES = 100 * 1024
_QC_SEED_ALLOWED_EXTS = (".md", ".txt")

_YT_COOKIES_FILE_PARAM = "video_editor_s3.yt_cookies_file_b64"
_YT_COOKIES_FILENAME_PARAM = "video_editor_s3.yt_cookies_filename"
_YT_COOKIES_UPLOADED_AT_PARAM = "video_editor_s3.yt_cookies_file_uploaded_at"
_YT_COOKIES_FILE_MAX_BYTES = 512 * 1024
_YT_COOKIES_ALLOWED_EXTS = (".txt",)


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
        default="moonshotai.kimi-k2.5",
        config_parameter="video_editor_s3.bedrock_model_id",
        help=(
            "Bedrock foundation-model ID used for prompt QC via the Converse API. "
            "Default 'moonshotai.kimi-k2.5' (Kimi K2.5, 256K context, available in "
            "ap-south-1/us-east-1/us-west-2/eu-north-1/eu-west-2/ap-northeast-1/"
            "ap-southeast-2/ap-southeast-3/ap-southeast-4/sa-east-1, "
            "Converse API + bearer-token auth). "
            "Other compatible IDs: 'moonshotai.kimi-k2-thinking', "
            "'amazon.nova-pro-v1:0', 'amazon.nova-lite-v1:0', "
            "'anthropic.claude-sonnet-4-5-20250929-v1:0', "
            "'anthropic.claude-3-5-sonnet-20241022-v2:0', "
            "'deepseek.deepseek-v3-2', 'deepseek.deepseek-r1'. "
            "All use the same Converse request shape."
        ),
    )
    video_editor_s3_bedrock_api_key = fields.Char(
        string="Bedrock API Key",
        config_parameter="video_editor_s3.bedrock_api_key",
    )
    video_editor_s3_qc_seed_file = fields.Binary(
        string="QC Seed Prompt File",
        help=(
            "Upload a .md or .txt file (UTF-8, max 100 KB) containing the QC "
            "seed prompt. Clear the file to fall back to the bundled default."
        ),
    )
    video_editor_s3_qc_seed_filename = fields.Char(
        string="QC Seed Prompt Filename",
        config_parameter="video_editor_s3.qc_seed_filename",
    )
    video_editor_s3_yt_cookies_file = fields.Binary(
        string="Admin Cookies File (Upload)",
        help=(
            "Upload a Netscape-format cookies.txt to be used as a fallback for "
            "all users who have not uploaded their own. Takes precedence over "
            "the 'Cookies File Path' field. Each user can override with their "
            "own file via Crowley Sourcing → My YouTube Cookies."
        ),
    )
    video_editor_s3_yt_cookies_filename = fields.Char(
        string="Admin Cookies Filename",
        config_parameter="video_editor_s3.yt_cookies_filename",
    )
    video_editor_s3_yt_cookies_uploaded_at = fields.Datetime(
        string="Admin Cookies Uploaded At",
        readonly=True,
    )
    video_editor_s3_yt_cookies_uploaded_at_display = fields.Char(
        string="Admin Cookies Uploaded",
        compute="_compute_video_editor_s3_yt_cookies_uploaded_at_display",
    )
    video_editor_s3_yt_proxy_url = fields.Char(
        string="YouTube Proxy URL",
        config_parameter="video_editor_s3.yt_proxy_url",
        help=(
            "Optional HTTP / HTTPS / SOCKS5 proxy used only for YouTube downloads, "
            "e.g. http://user:pass@host:8080 or socks5://host:1080. "
            "Use when the Odoo host's IP is flagged by YouTube."
        ),
    )

    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        b64 = ICP.get_param(_QC_SEED_FILE_PARAM) or ""
        res["video_editor_s3_qc_seed_file"] = b64.encode("ascii") if b64 else False
        cookies_b64 = ICP.get_param(_YT_COOKIES_FILE_PARAM) or ""
        res["video_editor_s3_yt_cookies_file"] = (
            cookies_b64.encode("ascii") if cookies_b64 else False
        )
        uploaded_at_raw = (ICP.get_param(_YT_COOKIES_UPLOADED_AT_PARAM) or "").strip()
        if uploaded_at_raw:
            try:
                res["video_editor_s3_yt_cookies_uploaded_at"] = (
                    fields.Datetime.from_string(uploaded_at_raw)
                )
            except (TypeError, ValueError):
                res["video_editor_s3_yt_cookies_uploaded_at"] = False
        else:
            res["video_editor_s3_yt_cookies_uploaded_at"] = False
        return res

    def set_values(self):
        self._validate_qc_seed_file()
        self._validate_yt_cookies_file()
        ICP = self.env["ir.config_parameter"].sudo()
        raw = self.video_editor_s3_qc_seed_file
        if raw:
            b64 = raw.decode("ascii") if isinstance(raw, bytes) else raw
            ICP.set_param(_QC_SEED_FILE_PARAM, b64)
        else:
            ICP.set_param(_QC_SEED_FILE_PARAM, "")
        cookies_raw = self.video_editor_s3_yt_cookies_file
        prev_b64 = (ICP.get_param(_YT_COOKIES_FILE_PARAM) or "").strip()
        if cookies_raw:
            b64 = cookies_raw.decode("ascii") if isinstance(cookies_raw, bytes) else cookies_raw
            ICP.set_param(_YT_COOKIES_FILE_PARAM, b64)
            if b64 != prev_b64:
                ICP.set_param(
                    _YT_COOKIES_UPLOADED_AT_PARAM,
                    fields.Datetime.to_string(fields.Datetime.now()),
                )
        else:
            ICP.set_param(_YT_COOKIES_FILE_PARAM, "")
            ICP.set_param(_YT_COOKIES_UPLOADED_AT_PARAM, "")
            try:
                dbname = (self.env.cr.dbname or "default").strip() or "default"
                youtube_downloader.unlink_cookies_blob("db/%s/admin_cookies" % dbname)
            except Exception:
                pass
        return super().set_values()

    @api.depends("video_editor_s3_yt_cookies_uploaded_at")
    def _compute_video_editor_s3_yt_cookies_uploaded_at_display(self):
        for rec in self:
            dt = rec.video_editor_s3_yt_cookies_uploaded_at
            rec.video_editor_s3_yt_cookies_uploaded_at_display = (
                fields.Datetime.to_string(dt) if dt else ""
            )

    def _validate_yt_cookies_file(self):
        raw_b64 = self.video_editor_s3_yt_cookies_file
        if not raw_b64:
            return
        try:
            content = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(_(
                "Admin cookies file is corrupted (base64 decode failed): %s"
            ) % exc) from exc
        if len(content) > _YT_COOKIES_FILE_MAX_BYTES:
            raise ValidationError(_(
                "Admin cookies file is too large (max %(max)d KB, got %(got)d KB)."
            ) % {
                "max": _YT_COOKIES_FILE_MAX_BYTES // 1024,
                "got": len(content) // 1024 + 1,
            })
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(_(
                "Admin cookies file must be UTF-8 text in Netscape format: %s"
            ) % exc) from exc
        first_line = (text.splitlines()[0].strip().lower() if text.strip() else "")
        if not any(m in first_line for m in youtube_downloader._NETSCAPE_HEADER_MARKERS):
            raise ValidationError(_(
                "Admin cookies file is not in Netscape format — the first line "
                "must start with '# Netscape HTTP Cookie File'. Re-export it "
                "with the 'Get cookies.txt LOCALLY' (Chrome/Edge) or "
                "'cookies.txt' (Firefox) extension."
            ))
        fn = (self.video_editor_s3_yt_cookies_filename or "").lower()
        if fn and not fn.endswith(_YT_COOKIES_ALLOWED_EXTS):
            raise ValidationError(_(
                "Admin cookies file must be a .txt file (got %s)."
            ) % fn)

    def _validate_qc_seed_file(self):
        raw_b64 = self.video_editor_s3_qc_seed_file
        if not raw_b64:
            return
        try:
            content = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(_(
                "QC seed prompt file is corrupted (base64 decode failed): %s"
            ) % exc) from exc
        if len(content) > _QC_SEED_FILE_MAX_BYTES:
            raise ValidationError(_(
                "QC seed prompt file is too large (max %d KB, got %d KB)."
            ) % (_QC_SEED_FILE_MAX_BYTES // 1024, len(content) // 1024 + 1))
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(_(
                "QC seed prompt file must be valid UTF-8 text: %s"
            ) % exc) from exc
        fn = (self.video_editor_s3_qc_seed_filename or "").lower()
        if fn and not fn.endswith(_QC_SEED_ALLOWED_EXTS):
            raise ValidationError(_(
                "QC seed prompt file must be .md or .txt (got %s)."
            ) % fn)

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


