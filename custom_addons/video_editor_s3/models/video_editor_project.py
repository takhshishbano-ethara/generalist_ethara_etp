# -*- coding: utf-8 -*-
import logging
import os
import shutil
from urllib.parse import urlparse

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import llm_qc, youtube_downloader

_logger = logging.getLogger(__name__)

PROJECT_STATES = [
    ("draft", "Draft"),
    ("processing", "Processing"),
    ("processed", "Processed"),
    ("exporting", "Exporting"),
    ("exported", "Done"),
    ("error", "Error"),
]


class VideoEditorProject(models.Model):
    _name = "video.editor.project"
    _description = "Crowly Sourcing project"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(
        string="Name",
        required=True,
        default=lambda self: self._default_name(),
        tracking=True,
    )
    project_name = fields.Char(
        string="Project Name",
    )
    s3_source_url = fields.Char(
        string="Source S3 URL",
        tracking=True,
    )
    s3_source_key = fields.Char(
        string="Source S3 Key",
        compute="_compute_s3_source_key",
        store=True,
    )
    source_metadata = fields.Json(string="Source Metadata")
    duration_seconds = fields.Float(
        string="Duration (s)",
        compute="_compute_source_summary",
        store=True,
    )
    resolution = fields.Char(
        string="Resolution",
        compute="_compute_source_summary",
        store=True,
    )
    source_size_mb = fields.Float(
        string="Source Size (MB)",
        compute="_compute_source_summary",
        store=True,
    )

    youtube_url = fields.Char(string="YouTube URL", tracking=True)
    youtube_video_id = fields.Char(
        string="YouTube Video ID",
        compute="_compute_youtube_video_id",
        store=True,
        index=True,
    )
    youtube_title = fields.Char(string="YouTube Title", readonly=True, tracking=True)
    youtube_channel = fields.Char(string="YouTube Channel", readonly=True)
    youtube_thumbnail_url = fields.Char(string="YouTube Thumbnail", readonly=True)
    youtube_duration_seconds = fields.Float(string="YouTube Duration (s)", readonly=True)
    youtube_ingested_at = fields.Datetime(string="YouTube Ingested At", readonly=True)

    prompt = fields.Text(string="Prompt")

    qc_score = fields.Float(string="QC Score", readonly=True)
    qc_expert_level = fields.Char(string="QC Expert Level", readonly=True)
    qc_quality = fields.Selection(
        [("pass", "Pass"), ("fail", "Fail")],
        string="QC Quality",
        readonly=True,
    )
    qc_reason = fields.Text(string="QC Reason", readonly=True)
    qc_issues = fields.Text(string="QC Issues", readonly=True)
    qc_evaluated_prompt = fields.Text(string="Evaluated Prompt", readonly=True)
    qc_corrected_prompt = fields.Text(string="Corrected Prompt", readonly=True)
    qc_evaluated_at = fields.Datetime(string="QC Evaluated At", readonly=True)

    category = fields.Selection(
        [("sports", "Sports")],
        string="Category",
    )
    style = fields.Selection(
        [
            ("casual", "Casual"),
            ("precise", "Precise"),
            ("exhaustive", "Exhaustive"),
            ("terse", "Terse"),
            ("creative", "Creative"),
            ("narrative", "Narrative"),
        ],
        string="Style",
    )

    editing_config = fields.Json(string="Editing Config")
    edited_file_path = fields.Char(string="Edited File")
    preview_file_path = fields.Char(string="Preview File")
    output_s3_url = fields.Char(string="Exported URL", tracking=True)

    trim_start_seconds = fields.Float(string="Trim Start (s)", readonly=True)
    trim_end_seconds = fields.Float(string="Trim End (s)", readonly=True)
    trim_duration_seconds = fields.Float(string="Trim Duration (s)", readonly=True)
    edited_resolution = fields.Char(string="Edited Resolution", readonly=True)
    edited_fps = fields.Float(string="Edited FPS", readonly=True)

    state = fields.Selection(
        PROJECT_STATES,
        default="draft",
        required=True,
        tracking=True,
        index=True,
    )

    assigned_to = fields.Many2one(
        "res.users",
        string="Assigned To",
        default=lambda self: self.env.user,
        tracking=True,
    )

    job_ids = fields.One2many(
        "video.editor.job", "project_id", string="Jobs",
    )
    active_job_id = fields.Many2one(
        "video.editor.job",
        string="Active Job",
        compute="_compute_active_job",
    )
    processing_log_ids = fields.One2many(
        "video.editor.processing.log", "project_id", string="Processing Logs",
    )

    @api.model
    def _default_name(self):
        seq = self.env["ir.sequence"].next_by_code("video.editor.project")
        return seq or _("Project")

    @api.depends("s3_source_url")
    def _compute_s3_source_key(self):
        for rec in self:
            url = (rec.s3_source_url or "").strip()
            if not url:
                rec.s3_source_key = False
                continue
            try:
                if url.startswith("s3://"):
                    _, key = url[len("s3://"):].split("/", 1)
                else:
                    parsed = urlparse(url)
                    key = parsed.path.lstrip("/")
                    host = parsed.netloc or ""
                    if host.endswith(".amazonaws.com") and (host.startswith("s3.") or host.startswith("s3-")):
                        if "/" in key:
                            _, key = key.split("/", 1)
                rec.s3_source_key = key or False
            except (ValueError, AttributeError):
                rec.s3_source_key = False

    @api.depends("source_metadata")
    def _compute_source_summary(self):
        for rec in self:
            meta = rec.source_metadata or {}
            rec.duration_seconds = float(meta.get("duration") or 0.0)
            rec.resolution = meta.get("resolution") or ""
            rec.source_size_mb = float(meta.get("size_bytes") or 0.0) / (1024 * 1024)

    @api.depends("job_ids.status")
    def _compute_active_job(self):
        for rec in self:
            running = rec.job_ids.filtered(lambda j: j.status in ("queued", "running"))
            rec.active_job_id = running[:1] if running else False

    @api.depends("youtube_url")
    def _compute_youtube_video_id(self):
        for rec in self:
            video_id, _normalized = youtube_downloader.parse_youtube_url(rec.youtube_url or "")
            rec.youtube_video_id = video_id or False

    @api.constrains("s3_source_url")
    def _check_s3_url(self):
        for rec in self:
            url = (rec.s3_source_url or "").strip()
            if not url:
                continue
            if url.startswith("s3://"):
                if "/" not in url[len("s3://"):]:
                    raise ValidationError(_("Invalid s3:// URL — missing key part."))
                continue
            if not url.startswith(("http://", "https://")):
                raise ValidationError(_(
                    "Source URL must be s3://… or https://… (got %s)") % url[:80])
            parsed = urlparse(url)
            if not parsed.netloc or not parsed.path:
                raise ValidationError(_("Invalid S3 URL."))

    @api.constrains("youtube_url")
    def _check_youtube_url(self):
        for rec in self:
            url = (rec.youtube_url or "").strip()
            if not url:
                continue
            video_id, _normalized = youtube_downloader.parse_youtube_url(url)
            if not video_id:
                raise ValidationError(_(
                    "Invalid YouTube URL. Expected youtube.com/watch?v=…, youtu.be/…, shorts, embed, or /v/ form."))

    def _kick_job(self, job_type, *, config=None):
        self.ensure_one()
        active = self.job_ids.filtered(lambda j: j.status in ("queued", "running"))
        if active:
            raise UserError(_(
                "Another job is already running for this project (#%d, %s)."
            ) % (active[0].id, active[0].job_type))
        job = self.env["video.editor.job"].create({
            "project_id": self.id,
            "job_type": job_type,
            "status": "queued",
            "config_json": config or {},
        })

        def _submit():
            job._submit_async()

        self.env.cr.postcommit.add(_submit)
        return job

    def action_render(self, config=None):
        self.ensure_one()
        if not self.s3_source_url:
            raise UserError(_("Set a source S3 URL first."))
        self.write({"state": "processing"})
        return self._kick_job("render", config=config)

    def action_preview(self, config=None):
        self.ensure_one()
        if not self.s3_source_url:
            raise UserError(_("Set a source S3 URL first."))
        return self._kick_job("preview", config=config)

    def action_export(self, s3_key=None):
        self.ensure_one()
        if not self.edited_file_path:
            raise UserError(_("Render the project before exporting."))
        cfg = {}
        if s3_key:
            cfg["s3_key"] = s3_key
        self.write({"state": "exporting"})
        return self._kick_job("export", config=cfg)

    def action_ingest_youtube(self):
        self.ensure_one()
        if not self.youtube_url:
            raise UserError(_("Set a YouTube URL first."))
        self._probe_youtube_or_raise()
        job = self._kick_job("youtube_ingest", config={"youtube_url": self.youtube_url})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("YouTube ingestion queued"),
                "message": _(
                    "Job #%s is downloading the video and uploading to S3. "
                    "Refresh this form when the job finishes — the Source S3 URL "
                    "will be populated automatically."
                ) % job.id,
                "type": "info",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_open_editor(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "video_editor_s3.video_editor",
            "name": self.name,
            "params": {"project_id": self.id},
        }

    def action_view_jobs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Jobs"),
            "res_model": "video.editor.job",
            "view_mode": "list,form",
            "domain": [("project_id", "=", self.id)],
            "context": {"default_project_id": self.id},
        }

    def _probe_youtube_or_raise(self):
        """Run the 2160p50/60 gate synchronously so a UserError surfaces as a modal popup."""
        self.ensure_one()
        if not self.youtube_url:
            return
        cfg = self.env["video.editor.s3.settings"].sudo().get_youtube_ingest_config()
        youtube_downloader.probe_and_select(
            self.youtube_url,
            cookies_path=cfg.get("cookies_path"),
            proxy_url=cfg.get("proxy_url"),
            cookies_from_browser=cfg.get("cookies_browser"),
        )

    def _maybe_auto_ingest_youtube(self):
        for rec in self:
            if not rec.youtube_url:
                continue
            if rec.youtube_ingested_at and rec.s3_source_url:
                continue
            if rec.job_ids.filtered(lambda j: j.status in ("queued", "running")):
                continue
            video_id, _normalized = youtube_downloader.parse_youtube_url(rec.youtube_url)
            if not video_id:
                continue
            rec._probe_youtube_or_raise()
            try:
                rec._kick_job("youtube_ingest", config={"youtube_url": rec.youtube_url})
            except UserError as exc:
                _logger.info("auto-ingest skipped for project %s: %s", rec.id, exc)

    def _maybe_run_prompt_qc(self):
        for rec in self:
            if not rec.prompt:
                continue
            if rec.job_ids.filtered(lambda j: j.job_type == "prompt_qc" and j.status in ("queued", "running")):
                continue
            try:
                rec._kick_job("prompt_qc", config={"prompt": rec.prompt})
            except UserError as exc:
                _logger.info("prompt_qc skipped for project %s: %s", rec.id, exc)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._maybe_auto_ingest_youtube()
        records._maybe_run_prompt_qc()
        return records

    def write(self, vals):
        res = super().write(vals)
        if "youtube_url" in vals:
            self._maybe_auto_ingest_youtube()
        if "prompt" in vals:
            self._maybe_run_prompt_qc()
        return res

    def unlink(self):
        storage = self.env["video.editor.s3.media.storage"].sudo()
        roots = []
        for rec in self:
            try:
                roots.append(storage.project_dir(rec))
            except UserError:
                continue
        result = super().unlink()
        for path in roots:
            try:
                if path and os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
            except OSError as exc:
                _logger.warning("project dir purge failed for %s: %s", path, exc)
        return result
