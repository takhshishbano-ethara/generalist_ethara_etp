# -*- coding: utf-8 -*-
import base64
import json
import logging
import os
import shutil
import tempfile
import threading
from datetime import datetime

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

import requests

from ..services import job_executor, llm_qc, s3_storage, youtube_downloader
from ..services.job_executor import JobCancelled

_logger = logging.getLogger(__name__)

JOB_TYPES = [
    ("render", "Render edited video"),
    ("preview", "Render preview"),
    ("export", "Export to S3"),
    ("youtube_ingest", "YouTube Ingest"),
    ("llm_qc", "LLM QC"),
    ("s3_probe", "Probe S3 source"),
]

JOB_STATES = [
    ("queued", "Queued"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
    ("cancelled", "Cancelled"),
]


class VideoEditorJob(models.Model):
    _name = "video.editor.job"
    _description = "Crowley Sourcing job"
    _order = "id desc"
    _rec_name = "display_name"

    project_id = fields.Many2one(
        "video.editor.project",
        string="Project",
        required=True,
        ondelete="cascade",
        index=True,
    )
    job_type = fields.Selection(JOB_TYPES, required=True, index=True)
    status = fields.Selection(JOB_STATES, default="queued", required=True, index=True)
    config_json = fields.Json(string="Config")
    progress_text = fields.Char(string="Progress")
    last_heartbeat = fields.Datetime(string="Heartbeat")
    error_message = fields.Text(string="Error")
    lambda_request_id = fields.Char(string="Lambda Request ID", index=True, readonly=True)
    last_lambda_log_ts = fields.Datetime(string="Last Lambda Log At", readonly=True)
    started_at = fields.Datetime(string="Started")
    finished_at = fields.Datetime(string="Finished")
    duration_ms = fields.Integer(string="Duration (ms)", compute="_compute_duration_ms", store=True)
    output_path = fields.Char(string="Output Path")
    output_s3_url = fields.Char(string="Output S3 URL")
    ffmpeg_command = fields.Text(string="Command")
    log = fields.Text(string="Log")
    display_name = fields.Char(compute="_compute_display_name")

    @api.depends("started_at", "finished_at")
    def _compute_duration_ms(self):
        for rec in self:
            if rec.started_at and rec.finished_at:
                delta = rec.finished_at - rec.started_at
                rec.duration_ms = int(delta.total_seconds() * 1000)
            else:
                rec.duration_ms = 0

    @api.depends("job_type", "status")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s #%s [%s]" % (
                dict(JOB_TYPES).get(rec.job_type, rec.job_type or "?"),
                rec.id or "new",
                dict(JOB_STATES).get(rec.status, rec.status or "?"),
            )

    def action_cancel(self):
        for rec in self:
            if rec.status not in ("queued", "running"):
                continue
            cancelled = job_executor.request_cancel(rec.id)
            if not cancelled and rec.status == "queued":
                rec.write({
                    "status": "cancelled",
                    "finished_at": fields.Datetime.now(),
                })
        return True

    def action_retry(self):
        self.ensure_one()
        if self.status not in ("failed", "cancelled"):
            raise UserError(_("Only failed or cancelled jobs can be retried."))
        sibling = self.project_id.job_ids.filtered(
            lambda j: j.id != self.id and j.status in ("queued", "running")
        )
        if sibling:
            raise UserError(_(
                "Another job is already queued/running for this project (#%d, %s)."
            ) % (sibling[0].id, sibling[0].job_type))
        self.write({
            "status": "queued",
            "error_message": False,
            "started_at": False,
            "finished_at": False,
            "progress_text": False,
        })
        job = self

        def _submit():
            job._submit_async()

        self.env.cr.postcommit.add(_submit)
        return True

    def action_view_log(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Job Log"),
            "res_model": "video.editor.job",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def _submit_async(self):
        self.ensure_one()
        db_name = self.env.cr.dbname
        uid = self.env.uid or SUPERUSER_ID
        submitted = job_executor.submit_job_async(db_name, uid, self.id, _run_job)
        if not submitted:
            raise UserError(_(
                "Too many concurrent video jobs are running. Try again shortly."
            ))

    @api.model
    def _cron_reap_stale_jobs(self):
        cutoff = fields.Datetime.subtract(
            fields.Datetime.now(), seconds=job_executor._HEARTBEAT_STALE_SECONDS
        )
        stale = self.search([
            ("status", "=", "running"),
            "|",
            ("last_heartbeat", "=", False),
            ("last_heartbeat", "<", cutoff),
        ])
        if not stale:
            return
        stale.write({
            "status": "failed",
            "error_message": _("Job marked failed: no heartbeat within %s seconds.") % job_executor._HEARTBEAT_STALE_SECONDS,
            "finished_at": fields.Datetime.now(),
        })


def _run_job(db, uid, job_id):
    with _cancel_event(job_id) as cancel_event:
        _set_running(db, job_id)
        job_type = _read_job_type(db, uid, job_id)
        if job_type == "render":
            _run_render(db, uid, job_id, cancel_event, preview=False)
        elif job_type == "preview":
            _run_render(db, uid, job_id, cancel_event, preview=True)
        elif job_type == "export":
            _run_export(db, uid, job_id, cancel_event)
        elif job_type == "youtube_ingest":
            _run_youtube_ingest(db, uid, job_id, cancel_event)
        elif job_type == "llm_qc":
            _run_llm_qc(db, uid, job_id, cancel_event)
        elif job_type == "s3_probe":
            _run_s3_probe(db, uid, job_id, cancel_event)
        else:
            raise UserError(_("Unknown job_type: %s") % job_type)
        _set_done(db, job_id)


class _CancelCtx:
    def __init__(self, job_id):
        self.job_id = int(job_id)
        self.event = None

    def __enter__(self):
        with job_executor._cancel_lock:
            self.event = job_executor._cancel_events.get(self.job_id)
        return self.event

    def __exit__(self, *a):
        return False


def _cancel_event(job_id):
    return _CancelCtx(job_id)


def _set_running(db, job_id):
    with Registry(db).cursor() as cr:
        job_executor._update_job(cr, job_id, {
            "status": "running",
            "started_at": datetime.utcnow(),
            "last_heartbeat": datetime.utcnow(),
        })
        cr.commit()


def _set_done(db, job_id):
    with Registry(db).cursor() as cr:
        job_executor._update_job(cr, job_id, {
            "status": "done",
            "finished_at": datetime.utcnow(),
        })
        cr.commit()
    job_executor._notify_job_completion(db, job_id)


def _read_job_type(db, uid, job_id):
    with Registry(db).cursor() as cr:
        cr.execute("SELECT job_type FROM video_editor_job WHERE id = %s", (job_id,))
        row = cr.fetchone()
        if not row:
            raise UserError(_("Job %s no longer exists.") % job_id)
        return row[0]


def _bump_heartbeat(db, job_id, text=None):
    with Registry(db).cursor() as cr:
        job_executor._heartbeat(cr, job_id, text)
        cr.commit()


def _run_render(db, uid, job_id, cancel_event, preview=False):
    project_id, src_abs, dst_abs, config = _read_render_context(db, uid, job_id, preview)
    job_executor._check_cancelled(cancel_event)

    if not preview:
        with Registry(db).cursor() as cr:
            env = api.Environment(cr, uid or SUPERUSER_ID, {})
            icp = env["ir.config_parameter"].sudo()
            use_lambda = (icp.get_param("video_editor_s3.use_lambda") or "").lower() in ("1", "true", "t", "yes")
            lambda_function_name = icp.get_param("video_editor_s3.lambda_function_name") or ""
            lambda_region = icp.get_param("video_editor_s3.lambda_region") or ""
            lambda_callback_base = (icp.get_param("video_editor_s3.lambda_callback_base_url") or "").rstrip("/")
            s3cfg = env["video.editor.s3.settings"].get_s3_config()
            export_prefix = env["video.editor.s3.settings"].get_default_export_prefix()
        if use_lambda:
            if not lambda_function_name or not lambda_region or not lambda_callback_base:
                raise UserError(_("Lambda pipeline is enabled but function name, region, or callback URL is unset."))
            from ..services import lambda_invoker
            ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
            out_key = "%s/project_%d/render_%s.mp4" % (export_prefix, project_id, ts)
            payload = {
                "op": "render",
                "job_id": job_id,
                "source_url": src_abs,
                "s3_bucket": s3cfg.get("bucket") or "",
                "s3_key": out_key,
                "config": config,
                "callback_url": "%s/video_editor_s3/callback/render" % lambda_callback_base,
            }
            _bump_heartbeat(db, job_id, "dispatched render to AWS Lambda")
            try:
                api_request_id = lambda_invoker.invoke_async(
                    lambda_function_name, lambda_region, payload,
                    access_key=s3cfg.get("access_key"),
                    secret_key=s3cfg.get("secret_key"),
                )
            except Exception as exc:
                raise UserError(_("Lambda render invoke failed: %s") % exc) from exc
            with Registry(db).cursor() as cr:
                job_executor._update_job(cr, job_id, {"progress_text": "lambda render dispatched (api_req=%s)" % api_request_id})
                cr.commit()
            raise job_executor.LambdaDispatched()

    _bump_heartbeat(db, job_id, "rendering preview" if preview else "rendering")

    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        job = env["video.editor.job"].browse(job_id)
        meta = env["video.editor.s3.ffmpeg.processor"].render(
            job, src_abs, dst_abs, config, preview=preview,
        )
        cr.commit()

    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        project = env["video.editor.project"].browse(project_id)
        storage = env["video.editor.s3.media.storage"]
        rel = storage.relative(dst_abs)
        job_executor._update_job(cr, job_id, {
            "output_path": rel,
            "ffmpeg_command": (meta or {}).get("ffmpeg_command", ""),
        })
        if preview:
            project.write({"preview_file_path": rel})
        else:
            trim = (config or {}).get("trim") or {}
            try:
                trim_start = float(trim.get("start") or 0.0)
            except (TypeError, ValueError):
                trim_start = 0.0
            try:
                trim_end = float(trim.get("end") or 0.0)
            except (TypeError, ValueError):
                trim_end = 0.0
            trim_duration = max(trim_end - trim_start, 0.0)
            try:
                with open(dst_abs, "rb") as _fh:
                    edited_blob_b64 = base64.b64encode(_fh.read())
                edited_blob_name = os.path.basename(dst_abs)
            except OSError as exc:
                _logger.warning(
                    "Render finished but reading %s for DB blob failed (%s); "
                    "export will rely on local disk only.",
                    dst_abs, exc,
                )
                edited_blob_b64 = False
                edited_blob_name = False
            project.write({
                "edited_file_path": rel,
                "edited_blob": edited_blob_b64,
                "edited_blob_filename": edited_blob_name,
                "editing_config": config,
                "state": "processed",
                "trim_start_seconds": trim_start,
                "trim_end_seconds": trim_end,
                "trim_duration_seconds": trim_duration,
                "edited_resolution": (meta or {}).get("resolution") or "",
                "edited_fps": float((meta or {}).get("fps") or 0.0),
            })
        cr.commit()


def _read_render_context(db, uid, job_id, preview):
    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        job = env["video.editor.job"].browse(job_id)
        if not job.exists():
            raise UserError(_("Job %s no longer exists.") % job_id)
        project = job.project_id
        if not project.s3_source_url:
            raise UserError(_("Project has no source S3 URL."))
        storage = env["video.editor.s3.media.storage"]
        src_input = _build_source_input(env, project)
        kind = "preview" if preview else "edited"
        dst_abs = storage.path_for(project, kind, version=1)
        config = job.config_json or project.editing_config or {}
        return project.id, src_input, dst_abs, dict(config or {})


def _build_source_input(env, project):
    url = (project.s3_source_url or "").strip()
    bucket, key = s3_storage.parse_s3_url(url)
    cfg = env["video.editor.s3.settings"].get_s3_config()
    if bucket and key and cfg.get("access_key") and cfg.get("secret_key"):
        return s3_storage.generate_presigned_url(
            {**cfg, "bucket": bucket}, key, expires_in=7200,
        )
    if url.startswith(("http://", "https://")):
        return url
    raise UserError(_(
        "Cannot resolve source URL %s — configure S3 credentials or use an https:// URL."
    ) % url[:120])


def _run_export(db, uid, job_id, cancel_event):
    project_id, local_abs, s3_key, cfg = _read_export_context(db, uid, job_id)
    job_executor._check_cancelled(cancel_event)

    if not os.path.exists(local_abs):
        _bump_heartbeat(db, job_id, "restoring local cache from DB blob")
        with Registry(db).cursor() as cr:
            env = api.Environment(cr, uid or SUPERUSER_ID, {})
            project = env["video.editor.project"].browse(project_id)
            blob = project.edited_blob
            if not blob:
                raise UserError(_(
                    "Edited video file is missing from local storage (%s) and no "
                    "database blob is available. Please re-render the project."
                ) % local_abs)
            raw_bytes = base64.b64decode(blob)
        os.makedirs(os.path.dirname(local_abs), exist_ok=True)
        with open(local_abs, "wb") as fh:
            fh.write(raw_bytes)

    _bump_heartbeat(db, job_id, "uploading to S3")
    url = s3_storage.upload_file(cfg, local_abs, s3_key)
    job_executor._check_cancelled(cancel_event)

    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        project = env["video.editor.project"].browse(project_id)
        project.write({
            "output_s3_url": url,
            "state": "exported",
            "edited_blob": False,
            "edited_blob_filename": False,
        })
        job_executor._update_job(cr, job_id, {"output_s3_url": url})
        cr.commit()


def _read_export_context(db, uid, job_id):
    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        job = env["video.editor.job"].browse(job_id)
        if not job.exists():
            raise UserError(_("Job %s no longer exists.") % job_id)
        project = job.project_id
        if not project.edited_file_path:
            raise UserError(_("Project has nothing to export yet — render first."))
        cfg = env["video.editor.s3.settings"].get_s3_config()
        if not s3_storage.is_configured(cfg):
            raise UserError(_("S3 settings are missing — configure bucket and credentials."))
        storage = env["video.editor.s3.media.storage"]
        local_abs = storage.absolute(project.edited_file_path)
        opts = job.config_json or {}
        s3_key = (opts.get("s3_key") or "").strip("/")
        if not s3_key:
            prefix = env["video.editor.s3.settings"].get_default_export_prefix()
            ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
            s3_key = "%s/project_%d/%s.mp4" % (prefix, project.id, ts)
        return project.id, local_abs, s3_key, cfg


def _log_yt(db, uid, job_id, project_id, level, operation, message="", duration_ms=0):
    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        env["video.editor.processing.log"].sudo().create({
            "project_id": project_id,
            "job_id": job_id,
            "level": level,
            "operation": operation,
            "message": (message or "")[:8000],
            "duration_ms": duration_ms,
        })
        cr.commit()


def _run_youtube_ingest(db, uid, job_id, cancel_event):
    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        job = env["video.editor.job"].browse(job_id)
        if not job.exists():
            raise UserError(_("Job %s no longer exists.") % job_id)
        project = job.project_id
        project_id = project.id
        opts = job.config_json or {}
        youtube_url = (opts.get("youtube_url") or project.youtube_url or "").strip()
        youtube_tier = (opts.get("tier") or project.youtube_tier or "").strip() or None
        if youtube_tier and youtube_tier not in youtube_downloader.YOUTUBE_TIERS:
            raise UserError(_("Unknown YouTube tier: %s") % youtube_tier)
        cfg = env["video.editor.s3.settings"].get_s3_config()
        if not s3_storage.is_configured(cfg):
            raise UserError(_("S3 settings are missing — configure bucket and credentials."))
        youtube_prefix = env["video.editor.s3.settings"].get_youtube_prefix()
        max_size_bytes = env["video.editor.s3.settings"].get_max_source_size_bytes()
        yt_ingest_cfg = env["video.editor.s3.settings"].get_youtube_ingest_config()
        yt_cookies_browser = yt_ingest_cfg.get("cookies_browser") or ""
        yt_cookies_path = yt_ingest_cfg.get("cookies_path") or ""
        yt_proxy = yt_ingest_cfg.get("proxy_url") or ""
        icp = env["ir.config_parameter"].sudo()
        use_lambda = (icp.get_param("video_editor_s3.use_lambda") or "").lower() in ("1", "true", "t", "yes")
        lambda_function_name = icp.get_param("video_editor_s3.lambda_function_name") or ""
        lambda_region = icp.get_param("video_editor_s3.lambda_region") or ""
        lambda_callback_base = (icp.get_param("video_editor_s3.lambda_callback_base_url") or "").rstrip("/")

    if use_lambda:
        if not lambda_function_name or not lambda_region or not lambda_callback_base:
            raise UserError(_("Lambda pipeline is enabled but function name, region, or callback URL is unset in Settings."))
        from ..services import lambda_invoker
        video_id, normalized = youtube_downloader.parse_youtube_url(youtube_url)
        if not video_id or not normalized:
            raise UserError(_("Invalid YouTube URL: %s") % youtube_url[:200])
        target_key = (
            "%s/%s_%s.mkv" % (youtube_prefix, video_id, youtube_tier)
            if youtube_tier
            else "%s/%s.mkv" % (youtube_prefix, video_id)
        )
        payload = {
            "op": "youtube_ingest",
            "job_id": job_id,
            "youtube_url": normalized,
            "tier": youtube_tier or "",
            "s3_bucket": cfg.get("bucket") or "",
            "s3_key": target_key,
            "callback_url": "%s/video_editor_s3/callback/youtube_ingest" % lambda_callback_base,
        }
        _bump_heartbeat(db, job_id, "dispatched to AWS Lambda")
        try:
            api_request_id = lambda_invoker.invoke_async(
                lambda_function_name, lambda_region, payload,
                access_key=cfg.get("access_key"),
                secret_key=cfg.get("secret_key"),
            )
        except Exception as exc:
            raise UserError(_("Lambda invoke failed: %s") % exc) from exc
        with Registry(db).cursor() as cr:
            job_executor._update_job(cr, job_id, {"progress_text": "lambda dispatched (api_req=%s)" % api_request_id})
            cr.commit()
        raise job_executor.LambdaDispatched()

    _bump_heartbeat(db, job_id, "validating YouTube URL")
    video_id, normalized = youtube_downloader.parse_youtube_url(youtube_url)
    if not video_id or not normalized:
        _log_yt(db, uid, job_id, project_id, "error", "youtube_validate",
                "Invalid YouTube URL: %s" % youtube_url[:200])
        raise UserError(_("Invalid YouTube URL: %s") % youtube_url[:200])
    _log_yt(db, uid, job_id, project_id, "info", "youtube_validate",
            "video_id=%s normalized=%s" % (video_id, normalized))
    job_executor._check_cancelled(cancel_event)

    target_key = (
        "%s/%s_%s.mkv" % (youtube_prefix, video_id, youtube_tier)
        if youtube_tier
        else "%s/%s.mkv" % (youtube_prefix, video_id)
    )

    if s3_storage.head_object_exists(cfg, target_key):
        _log_yt(db, uid, job_id, project_id, "info", "youtube_dedup_hit",
                "S3 key already exists: %s" % target_key)
        _bump_heartbeat(db, job_id, "reusing existing S3 object")
        chosen_format = None
        if youtube_tier:
            try:
                probe_info, chosen_format = youtube_downloader.probe_and_select(
                    normalized,
                    tier=youtube_tier,
                    cookies_path=yt_cookies_path,
                    proxy_url=yt_proxy,
                    cookies_from_browser=yt_cookies_browser,
                )
                metadata = {
                    "title": probe_info.get("title") or "",
                    "channel": probe_info.get("channel") or probe_info.get("uploader") or "",
                    "thumbnail": probe_info.get("thumbnail") or "",
                    "duration_seconds": float(probe_info.get("duration") or 0.0),
                }
            except Exception as exc:
                _logger.warning("youtube tier-aware probe failed during dedup: %s", exc)
                chosen_format = None
                metadata = None
        if chosen_format is None:
            try:
                metadata = youtube_downloader.extract_metadata(
                    normalized,
                    cookies_path=yt_cookies_path,
                    proxy_url=yt_proxy,
                    cookies_from_browser=yt_cookies_browser,
                )
            except Exception as exc:
                _logger.warning("youtube_metadata fetch failed during dedup: %s", exc)
                metadata = {"title": "", "channel": "", "thumbnail": "", "duration_seconds": 0.0,
                            "width": 0, "height": 0, "fps": 0.0, "vcodec": ""}
        s3_url = s3_storage.build_url(cfg["bucket"], cfg["region"], target_key)
        if chosen_format is not None:
            yt_width = int(chosen_format.get("width") or 0)
            yt_height = int(chosen_format.get("height") or 0)
            yt_fps = float(chosen_format.get("fps") or 0.0)
            vcodec_raw = chosen_format.get("vcodec") or ""
            yt_vcodec = vcodec_raw.split(".")[0] if vcodec_raw else ""
        else:
            yt_width = int(metadata.get("width") or 0)
            yt_height = int(metadata.get("height") or 0)
            yt_fps = float(metadata.get("fps") or 0.0)
            yt_vcodec = metadata.get("vcodec") or ""
        yt_resolution = "%dx%d" % (yt_width, yt_height) if yt_width and yt_height else ""
        with Registry(db).cursor() as cr:
            env = api.Environment(cr, uid or SUPERUSER_ID, {})
            project = env["video.editor.project"].browse(project_id)
            project.write({
                "s3_source_url": s3_url,
                "source_metadata": {
                    "duration": float(metadata.get("duration_seconds") or 0.0),
                    "size_bytes": 0,
                    "width": yt_width,
                    "height": yt_height,
                    "resolution": yt_resolution,
                    "fps": yt_fps,
                    "codec": yt_vcodec,
                    "youtube_width": yt_width,
                    "youtube_height": yt_height,
                    "youtube_fps": yt_fps,
                    "youtube_vcodec": yt_vcodec,
                },
                "youtube_title": metadata.get("title") or "",
                "youtube_channel": metadata.get("channel") or "",
                "youtube_thumbnail_url": metadata.get("thumbnail") or "",
                "youtube_duration_seconds": float(metadata.get("duration_seconds") or 0.0),
                "youtube_resolution": yt_resolution or "",
                "youtube_fps": float(yt_fps or 0.0),
                "youtube_tier": youtube_tier or False,
                "youtube_ingested_at": fields.Datetime.now(),
            })
            job_executor._update_job(cr, job_id, {"output_s3_url": s3_url})
            cr.commit()
        return

    _log_yt(db, uid, job_id, project_id, "info", "youtube_dedup_miss",
            "S3 key not present, downloading: %s" % target_key)

    probe_info, chosen_format = youtube_downloader.probe_and_select(
        normalized,
        tier=youtube_tier,
        cookies_path=yt_cookies_path,
        proxy_url=yt_proxy,
        cookies_from_browser=yt_cookies_browser,
    )
    metadata = {
        "video_id": probe_info.get("id") or video_id,
        "title": probe_info.get("title") or "",
        "channel": probe_info.get("channel") or probe_info.get("uploader") or "",
        "thumbnail": probe_info.get("thumbnail") or "",
        "duration_seconds": float(probe_info.get("duration") or 0.0),
    }
    _log_yt(db, uid, job_id, project_id, "info", "youtube_metadata",
            "title=%s channel=%s duration=%s chosen=%sp%s/%s" % (
                (metadata.get("title") or "")[:200],
                (metadata.get("channel") or "")[:100],
                metadata.get("duration_seconds"),
                chosen_format.get("height"),
                chosen_format.get("fps"),
                chosen_format.get("format_id"),
            ))

    job_executor._check_cancelled(cancel_event)
    tempdir = tempfile.mkdtemp(prefix="video_editor_s3_yt_")
    progress_state = {"last_pct": -1}

    def progress_cb(downloaded, total, status):
        if total and total > 0:
            pct = int((downloaded / total) * 100)
            if pct >= progress_state["last_pct"] + 5:
                progress_state["last_pct"] = pct
                _bump_heartbeat(db, job_id, "downloading %d%%" % pct)

    _log_yt(db, uid, job_id, project_id, "info", "youtube_download_start",
            "url=%s max_bytes=%s tempdir=%s" % (normalized, max_size_bytes, tempdir))
    _bump_heartbeat(db, job_id, "downloading from YouTube")

    try:
        try:
            abs_path, info, _chosen = youtube_downloader.download_to_tempdir(
                normalized,
                tempdir,
                info=probe_info,
                chosen_format=chosen_format,
                max_size_bytes=max_size_bytes,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
                cancel_exception=JobCancelled,
                cookies_path=yt_cookies_path,
                proxy_url=yt_proxy,
                cookies_from_browser=yt_cookies_browser,
            )
        except JobCancelled:
            _log_yt(db, uid, job_id, project_id, "warning", "youtube_ingest_cancelled",
                    "Download cancelled before completion.")
            raise
        except Exception as exc:
            _log_yt(db, uid, job_id, project_id, "error", "youtube_download_failed",
                    "%s" % str(exc)[:2000])
            raise

        _log_yt(db, uid, job_id, project_id, "info", "youtube_download_done",
                "path=%s size=%d" % (abs_path, os.path.getsize(abs_path)))

        with Registry(db).cursor() as cr:
            env = api.Environment(cr, uid or SUPERUSER_ID, {})
            probe = env["video.editor.s3.ffmpeg.processor"].probe(abs_path) or {}
        if not probe.get("duration") or not probe.get("width") or not probe.get("height"):
            _log_yt(db, uid, job_id, project_id, "error", "youtube_download_failed",
                    "Downloaded file has no playable video stream: %s" % probe)
            raise UserError(_(
                "YouTube download produced a file with no video stream "
                "(probe=%s). Aborting before S3 upload."
            ) % probe)
        min_height_floor = (
            youtube_downloader.YOUTUBE_TIERS[youtube_tier]["min_height"]
            if youtube_tier
            else 2160
        )
        gate_label = youtube_tier or "2160p50/60"
        if int(probe.get("height") or 0) < min_height_floor:
            _log_yt(db, uid, job_id, project_id, "error", "youtube_quality_assert",
                    "Downloaded file height=%s below %s floor; refusing upload."
                    % (probe.get("height"), min_height_floor))
            raise UserError(_(
                "Downloaded video is %(h)spx tall but the minimum requirement "
                "is %(gate)s. Aborting before S3 upload."
            ) % {"h": probe.get("height"), "gate": gate_label})

        if not metadata.get("title"):
            metadata = {
                "video_id": info.get("id") or video_id,
                "title": info.get("title") or "",
                "channel": info.get("channel") or info.get("uploader") or "",
                "thumbnail": info.get("thumbnail") or "",
                "duration_seconds": float(info.get("duration") or 0.0),
            }

        job_executor._check_cancelled(cancel_event)
        _bump_heartbeat(db, job_id, "uploading to S3")
        _log_yt(db, uid, job_id, project_id, "info", "s3_upload_start",
                "key=%s" % target_key)
        s3_url = s3_storage.upload_file(cfg, abs_path, target_key)
        _log_yt(db, uid, job_id, project_id, "info", "s3_upload_done",
                "url=%s" % s3_url)

        yt_width = int(chosen_format.get("width") or probe_info.get("width") or 0)
        yt_height = int(chosen_format.get("height") or probe_info.get("height") or 0)
        yt_fps = float(chosen_format.get("fps") or probe_info.get("fps") or 0.0)
        yt_vcodec_raw = chosen_format.get("vcodec") or probe_info.get("vcodec") or ""
        yt_vcodec = yt_vcodec_raw.split(".")[0] if yt_vcodec_raw else ""
        yt_resolution = "%dx%d" % (yt_width, yt_height) if yt_width and yt_height else ""

        probe_width = int(probe.get("width") or 0)
        probe_height = int(probe.get("height") or 0)
        probe_fps = float(probe.get("fps") or 0.0)
        final_width = probe_width or yt_width
        final_height = probe_height or yt_height
        final_fps = probe_fps or yt_fps
        final_resolution = probe.get("resolution") or (
            "%dx%d" % (final_width, final_height) if final_width and final_height else ""
        )
        final_codec = probe.get("codec") or yt_vcodec

        with Registry(db).cursor() as cr:
            env = api.Environment(cr, uid or SUPERUSER_ID, {})
            project = env["video.editor.project"].browse(project_id)
            project.write({
                "s3_source_url": s3_url,
                "source_metadata": {
                    "duration": float(metadata.get("duration_seconds") or 0.0),
                    "size_bytes": os.path.getsize(abs_path),
                    "width": final_width,
                    "height": final_height,
                    "resolution": final_resolution,
                    "fps": final_fps,
                    "codec": final_codec,
                    "youtube_width": yt_width,
                    "youtube_height": yt_height,
                    "youtube_fps": yt_fps,
                    "youtube_vcodec": yt_vcodec,
                },
                "youtube_title": metadata.get("title") or "",
                "youtube_channel": metadata.get("channel") or "",
                "youtube_thumbnail_url": metadata.get("thumbnail") or "",
                "youtube_duration_seconds": float(metadata.get("duration_seconds") or 0.0),
                "youtube_resolution": yt_resolution or "",
                "youtube_fps": float(yt_fps or 0.0),
                "youtube_tier": youtube_tier or False,
                "youtube_ingested_at": fields.Datetime.now(),
            })
            job_executor._update_job(cr, job_id, {"output_s3_url": s3_url})
            cr.commit()
    finally:
        try:
            shutil.rmtree(tempdir, ignore_errors=True)
            _log_yt(db, uid, job_id, project_id, "info", "cleanup",
                    "Removed tempdir %s" % tempdir)
        except Exception as exc:
            _logger.warning("Cleanup tempdir failed: %s", exc)


def _run_s3_probe(db, uid, job_id, cancel_event):
    job_executor._check_cancelled(cancel_event)
    _bump_heartbeat(db, job_id, "probing source")

    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        job = env["video.editor.job"].browse(job_id)
        project = job.project_id
        if not project.s3_source_url:
            raise UserError(_("Project has no source S3 URL."))
        src_input = _build_source_input(env, project)
        job_executor._check_cancelled(cancel_event)
        meta = env["video.editor.s3.ffmpeg.processor"].probe(src_input) or {}
        existing = dict(project.source_metadata or {})
        existing.update({
            "duration": float(meta.get("duration") or 0.0),
            "resolution": meta.get("resolution") or "",
            "fps": float(meta.get("fps") or 0.0),
            "size_bytes": int(meta.get("size_bytes") or 0),
            "width": int(meta.get("width") or 0),
            "height": int(meta.get("height") or 0),
            "codec": meta.get("codec") or "",
        })
        project.write({"source_metadata": existing})
        cr.commit()


def _verdict_to_field(v):
    v = (v or "").upper()
    if v == "PASS":
        return "pass"
    if v == "FAIL":
        return "fail"
    if v == "FLAG":
        return "flag"
    return False


def _run_llm_qc(db, uid, job_id, cancel_event):
    job_executor._check_cancelled(cancel_event)
    _bump_heartbeat(db, job_id, "preparing LLM QC")

    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        job = env["video.editor.job"].browse(job_id)
        if not job.exists():
            raise UserError(_("Job %s no longer exists.") % job_id)
        project = job.project_id
        project_id = project.id

        item_id = str(project.id)
        category = project.category or ""
        sub_category = project.sub_category or ""
        topic = getattr(project, "topic_name", "") or getattr(project, "topic", "") or ""
        prompt_text = (project.prompt or "").strip()
        style = project.style or ""

        meta = project.source_metadata or {}
        resolution = (meta.get("resolution") or project.resolution or "").strip()
        try:
            duration_seconds = float(meta.get("duration") or project.duration_seconds or 0.0)
        except (TypeError, ValueError):
            duration_seconds = 0.0
        try:
            fps = float(meta.get("fps") or project.source_fps or 0.0)
        except (TypeError, ValueError):
            fps = 0.0

        settings = env["video.editor.s3.settings"]
        llm_cfg = settings.get_llm_qc_config()
        seed_prompt = settings.get_llm_qc_seed_prompt()

        trimmed_url = None
        raw_url = (project.output_s3_url or "").strip()
        if not raw_url:
            raise UserError(_(
                "No trimmed S3 URL on this project. Render and export the "
                "trimmed clip to S3 before running LLM QC."
            ))
        if raw_url.startswith("s3://"):
            bucket, key = s3_storage.parse_s3_url(raw_url)
            cfg = env["video.editor.s3.settings"].get_s3_config()
            if bucket and key and cfg.get("access_key") and cfg.get("secret_key"):
                trimmed_url = s3_storage.generate_presigned_url(
                    {**cfg, "bucket": bucket}, key, expires_in=7200,
                )
        elif raw_url.startswith(("http://", "https://")):
            trimmed_url = raw_url
        if not trimmed_url:
            raise UserError(_(
                "Could not resolve trimmed S3 URL for LLM QC: %s"
            ) % raw_url[:160])

    if not prompt_text:
        raise UserError(_("Prompt is empty - write a prompt before running LLM QC."))
    if not llm_cfg.get("api_key"):
        _log_yt(db, uid, job_id, project_id, "error", "llm_qc_failed",
                "OpenRouter API key missing - configure under Settings > Crowly Sourcing > LLM QC.")
        raise UserError(_("OpenRouter API key missing - configure under Settings > Crowly Sourcing > LLM QC."))

    job_executor._check_cancelled(cancel_event)
    _bump_heartbeat(db, job_id, "downloading trimmed video from S3")

    video_bytes = None
    video_filename = "video.mp4"
    try:
        resp = requests.get(trimmed_url, stream=True, timeout=(15, 600))
        resp.raise_for_status()
        video_bytes = resp.content
    except requests.RequestException as exc:
        raise UserError(_(
            "Failed to download trimmed video from S3 for LLM QC: %s"
        ) % exc) from exc
    from urllib.parse import urlparse
    path = urlparse(trimmed_url).path
    if path:
        video_filename = os.path.basename(path) or video_filename

    if not video_bytes:
        raise UserError(_("Could not obtain trimmed video bytes from S3 for LLM QC."))

    job_executor._check_cancelled(cancel_event)
    _bump_heartbeat(db, job_id, "calling LLM QC reviewer")
    _log_yt(db, uid, job_id, project_id, "info", "llm_qc_start",
            "model=%s prompt_chars=%d video_bytes=%d category=%s style=%s" % (
                llm_cfg.get("model_id"), len(prompt_text), len(video_bytes),
                category, style,
            ))

    try:
        result = llm_qc.evaluate_llm_qc(
            item_id=item_id,
            category=category,
            sub_category=sub_category,
            description="",
            topic=topic,
            prompt=prompt_text,
            resolution=resolution,
            duration_seconds=duration_seconds,
            style=style,
            fps=fps,
            video_bytes=video_bytes,
            video_filename=video_filename,
            seed_prompt=seed_prompt,
            openrouter_api_key=llm_cfg["api_key"],
            model_id=llm_cfg.get("model_id") or llm_qc.DEFAULT_MODEL_ID,
            max_attempts=3,
            max_tokens=32000,
        )
    except JobCancelled:
        raise
    except Exception as exc:
        _log_yt(db, uid, job_id, project_id, "error", "llm_qc_failed",
                "%s" % str(exc)[:2000])
        raise

    _log_yt(db, uid, job_id, project_id, "info", "llm_qc_done",
            "qc_result=%s failure_reason=%s" % (
                result.get("qc_result"),
                (result.get("failure_reason") or "")[:200],
            ))

    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        project = env["video.editor.project"].browse(project_id)
        project.write({
            "llm_qc_result": _verdict_to_field(result.get("qc_result")),
            "llm_failure_reason": result.get("failure_reason") or "",
            "llm_fixed_prompt": result.get("fixed_prompt") or "",
            "llm_evaluated_at": fields.Datetime.now(),
        })
        cr.commit()
