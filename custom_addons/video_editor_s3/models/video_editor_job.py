# -*- coding: utf-8 -*-
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

from ..services import job_executor, llm_qc, s3_storage, youtube_downloader
from ..services.job_executor import JobCancelled

_logger = logging.getLogger(__name__)

JOB_TYPES = [
    ("render", "Render edited video"),
    ("preview", "Render preview"),
    ("export", "Export to S3"),
    ("youtube_ingest", "YouTube Ingest"),
    ("prompt_qc", "Prompt QC"),
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
    _description = "Video Editor S3 job"
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
        self.write({
            "status": "queued",
            "error_message": False,
            "started_at": False,
            "finished_at": False,
            "progress_text": False,
        })
        self._submit_async()
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
        elif job_type == "prompt_qc":
            _run_prompt_qc(db, uid, job_id, cancel_event)
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
            project.write({
                "edited_file_path": rel,
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
    _bump_heartbeat(db, job_id, "uploading to S3")

    url = s3_storage.upload_file(cfg, local_abs, s3_key)
    job_executor._check_cancelled(cancel_event)

    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        project = env["video.editor.project"].browse(project_id)
        project.write({
            "output_s3_url": url,
            "state": "exported",
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
        cfg = env["video.editor.s3.settings"].get_s3_config()
        if not s3_storage.is_configured(cfg):
            raise UserError(_("S3 settings are missing — configure bucket and credentials."))
        youtube_prefix = env["video.editor.s3.settings"].get_youtube_prefix()
        max_size_bytes = env["video.editor.s3.settings"].get_max_source_size_bytes()
        yt_ingest_cfg = env["video.editor.s3.settings"].get_youtube_ingest_config()
        yt_cookies_browser = yt_ingest_cfg.get("cookies_browser") or ""
        yt_cookies_path = yt_ingest_cfg.get("cookies_path") or ""
        yt_proxy = yt_ingest_cfg.get("proxy_url") or ""

    _bump_heartbeat(db, job_id, "validating YouTube URL")
    video_id, normalized = youtube_downloader.parse_youtube_url(youtube_url)
    if not video_id or not normalized:
        _log_yt(db, uid, job_id, project_id, "error", "youtube_validate",
                "Invalid YouTube URL: %s" % youtube_url[:200])
        raise UserError(_("Invalid YouTube URL: %s") % youtube_url[:200])
    _log_yt(db, uid, job_id, project_id, "info", "youtube_validate",
            "video_id=%s normalized=%s" % (video_id, normalized))
    job_executor._check_cancelled(cancel_event)

    target_key = "%s/%s.mkv" % (youtube_prefix, video_id)

    if s3_storage.head_object_exists(cfg, target_key):
        _log_yt(db, uid, job_id, project_id, "info", "youtube_dedup_hit",
                "S3 key already exists: %s" % target_key)
        _bump_heartbeat(db, job_id, "reusing existing S3 object")
        try:
            metadata = youtube_downloader.extract_metadata(
                normalized,
                cookies_path=yt_cookies_path,
                proxy_url=yt_proxy,
                cookies_from_browser=yt_cookies_browser,
            )
        except Exception as exc:
            _logger.warning("youtube_metadata fetch failed during dedup: %s", exc)
            metadata = {"title": "", "channel": "", "thumbnail": "", "duration_seconds": 0.0}
        s3_url = s3_storage.build_url(cfg["bucket"], cfg["region"], target_key)
        with Registry(db).cursor() as cr:
            env = api.Environment(cr, uid or SUPERUSER_ID, {})
            project = env["video.editor.project"].browse(project_id)
            project.write({
                "s3_source_url": s3_url,
                "youtube_title": metadata.get("title") or "",
                "youtube_channel": metadata.get("channel") or "",
                "youtube_thumbnail_url": metadata.get("thumbnail") or "",
                "youtube_duration_seconds": float(metadata.get("duration_seconds") or 0.0),
                "youtube_ingested_at": fields.Datetime.now(),
            })
            job_executor._update_job(cr, job_id, {"output_s3_url": s3_url})
            cr.commit()
        return

    _log_yt(db, uid, job_id, project_id, "info", "youtube_dedup_miss",
            "S3 key not present, downloading: %s" % target_key)

    probe_info, chosen_format = youtube_downloader.probe_and_select(
        normalized,
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
        if int(probe.get("height") or 0) < 2160:
            _log_yt(db, uid, job_id, project_id, "error", "youtube_quality_assert",
                    "Downloaded file height=%s below 2160 floor; refusing upload."
                    % probe.get("height"))
            raise UserError(_(
                "Downloaded video is %(h)spx tall but the minimum requirement "
                "is 2160p50/60. Aborting before S3 upload."
            ) % {"h": probe.get("height")})

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

        with Registry(db).cursor() as cr:
            env = api.Environment(cr, uid or SUPERUSER_ID, {})
            project = env["video.editor.project"].browse(project_id)
            project.write({
                "s3_source_url": s3_url,
                "source_metadata": {
                    "duration": float(metadata.get("duration_seconds") or 0.0),
                    "size_bytes": os.path.getsize(abs_path),
                },
                "youtube_title": metadata.get("title") or "",
                "youtube_channel": metadata.get("channel") or "",
                "youtube_thumbnail_url": metadata.get("thumbnail") or "",
                "youtube_duration_seconds": float(metadata.get("duration_seconds") or 0.0),
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


def _run_prompt_qc(db, uid, job_id, cancel_event):
    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        job = env["video.editor.job"].browse(job_id)
        if not job.exists():
            raise UserError(_("Job %s no longer exists.") % job_id)
        project = job.project_id
        project_id = project.id
        opts = job.config_json or {}
        prompt = (opts.get("prompt") or project.prompt or "").strip()
        if not prompt:
            raise UserError(_("Prompt is empty — nothing to evaluate."))
        bedrock_cfg = env["video.editor.s3.settings"].get_bedrock_config()
        seed_prompt = env["video.editor.s3.settings"].get_qc_seed_prompt()

    if not bedrock_cfg.get("access_key") or not bedrock_cfg.get("secret_key"):
        _log_yt(db, uid, job_id, project_id, "error", "prompt_qc_failed",
                "Bedrock credentials missing — configure under Settings.")
        raise UserError(_("Bedrock credentials missing — configure under Settings."))
    if not bedrock_cfg.get("model_id"):
        raise UserError(_("Bedrock model_id missing — configure under Settings."))

    _bump_heartbeat(db, job_id, "evaluating prompt")
    _log_yt(db, uid, job_id, project_id, "info", "prompt_qc_start",
            "model=%s region=%s prompt_chars=%d" % (
                bedrock_cfg.get("model_id"),
                bedrock_cfg.get("region"),
                len(prompt),
            ))

    try:
        result = llm_qc.evaluate_prompt(
            prompt=prompt,
            seed_prompt=seed_prompt,
            access_key=bedrock_cfg["access_key"],
            secret_key=bedrock_cfg["secret_key"],
            region=bedrock_cfg.get("region") or llm_qc.DEFAULT_REGION,
            model_id=bedrock_cfg.get("model_id") or llm_qc.DEFAULT_MODEL_ID,
        )
    except JobCancelled:
        raise
    except Exception as exc:
        _log_yt(db, uid, job_id, project_id, "error", "prompt_qc_failed",
                "%s" % str(exc)[:2000])
        raise

    _log_yt(db, uid, job_id, project_id, "info", "prompt_qc_done",
            "score=%s quality=%s expert_level=%s" % (
                result.get("score"),
                result.get("quality"),
                result.get("expert_level"),
            ))

    raw_json = (result.get("raw_json") or "")[:8000]
    with Registry(db).cursor() as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        project = env["video.editor.project"].browse(project_id)
        project.write({
            "qc_score": float(result.get("score") or 0.0),
            "qc_expert_level": (result.get("expert_level") or "")[:64],
            "qc_quality": result.get("quality") if result.get("quality") in ("pass", "fail") else "fail",
            "qc_reason": result.get("reason") or "",
            "qc_issues": result.get("issues") or "",
            "qc_evaluated_prompt": prompt,
            "qc_evaluated_at": fields.Datetime.now(),
        })
        job_executor._update_job(cr, job_id, {"output_path": raw_json})
        cr.commit()
