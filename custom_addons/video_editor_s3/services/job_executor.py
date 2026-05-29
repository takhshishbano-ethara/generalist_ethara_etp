# -*- coding: utf-8 -*-
import atexit
import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from odoo import SUPERUSER_ID, _, api
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

_MAX_WORKER_THREADS = int(os.environ.get("VIDEO_EDITOR_S3_MAX_WORKERS", "100") or 100)
_MAX_CONCURRENT_JOBS = int(os.environ.get("VIDEO_EDITOR_S3_MAX_CONCURRENT", "100") or 100)
_HEARTBEAT_INTERVAL_SECONDS = 10
_HEARTBEAT_STALE_SECONDS = 36000
_MAX_LOG_SIZE = 2_000_000

_executor = ThreadPoolExecutor(
    max_workers=_MAX_WORKER_THREADS,
    thread_name_prefix="video_editor_s3",
)
_semaphore = threading.Semaphore(_MAX_CONCURRENT_JOBS)

_cancel_events = {}
_cancel_lock = threading.Lock()


def _shutdown():
    try:
        _executor.shutdown(wait=True, cancel_futures=True)
    except Exception:
        pass


atexit.register(_shutdown)


class JobCancelled(Exception):
    pass


def _register_cancel_event(job_id):
    event = threading.Event()
    with _cancel_lock:
        _cancel_events[int(job_id)] = event
    return event


def _unregister_cancel_event(job_id):
    with _cancel_lock:
        _cancel_events.pop(int(job_id), None)


def request_cancel(job_id):
    with _cancel_lock:
        event = _cancel_events.get(int(job_id))
    if event:
        event.set()
        return True
    return False


def _check_cancelled(event):
    if event and event.is_set():
        raise JobCancelled()


def _open_cursor(db_name):
    return Registry(db_name).cursor()


def _update_job(cr, job_id, vals):
    if not vals:
        return
    allowed = {
        "status", "progress_text", "last_heartbeat", "error_message",
        "started_at", "finished_at", "duration_ms", "output_path",
        "output_s3_url", "ffmpeg_command",
    }
    cols, params = [], []
    for k, v in vals.items():
        if k not in allowed:
            continue
        cols.append("%s = %%s" % k)
        params.append(v)
    if not cols:
        return
    params.append(int(job_id))
    cr.execute(
        "UPDATE video_editor_job SET %s WHERE id = %%s" % ", ".join(cols),
        params,
    )


def _append_log(cr, job_id, message):
    if not message:
        return
    cr.execute(
        "UPDATE video_editor_job "
        "SET log = RIGHT(COALESCE(log, '') || %s, %s) "
        "WHERE id = %s",
        (message, _MAX_LOG_SIZE, int(job_id)),
    )


def _heartbeat(cr, job_id, progress_text=None):
    from datetime import datetime
    vals = {"last_heartbeat": datetime.utcnow()}
    if progress_text is not None:
        vals["progress_text"] = progress_text[:255]
    _update_job(cr, job_id, vals)


def _safe_worker(fn):
    def wrapper(db, uid, job_id, *args, **kwargs):
        try:
            try:
                return fn(db, uid, job_id, *args, **kwargs)
            except JobCancelled:
                _logger.info("video_editor_s3 job %s cancelled", job_id)
                _mark_status(db, job_id, "cancelled", error=None)
            except Exception as exc:
                _logger.exception("video_editor_s3 job %s failed", job_id)
                _mark_status(db, job_id, "failed", error=("%s\n%s" % (exc, traceback.format_exc()))[-2000:])
        finally:
            _unregister_cancel_event(job_id)
            try:
                _semaphore.release()
            except ValueError:
                pass
    return wrapper


def _mark_status(db, job_id, status, error=None):
    from datetime import datetime
    try:
        with _open_cursor(db) as cr:
            vals = {"status": status, "finished_at": datetime.utcnow()}
            if error:
                vals["error_message"] = error
            _update_job(cr, job_id, vals)
            cr.commit()
    except Exception:
        _logger.exception("failed to mark job %s as %s", job_id, status)
    _notify_job_completion(db, job_id)


_JOB_TYPE_LABELS = {
    "render": "Render",
    "preview": "Preview",
    "export": "Export",
    "youtube_ingest": "YouTube ingest",
    "prompt_qc": "Prompt QC",
    "s3_probe": "S3 probe",
}


def _notify_job_completion(db, job_id):
    try:
        with _open_cursor(db) as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            job = env["video.editor.job"].browse(job_id)
            if not job.exists():
                return
            user = job.create_uid
            if not user or not user.active or not user.partner_id:
                return
            payload = _build_notification_payload(job)
            if payload is None:
                return
            user._bus_send("simple_notification", payload)
            cr.commit()
    except Exception:
        _logger.exception("notify_job_completion failed for job %s", job_id)


def _build_notification_payload(job):
    status = job.status
    job_type = job.job_type
    project = job.project_id
    label = _JOB_TYPE_LABELS.get(job_type, job_type or "Job")
    if status == "done":
        if job_type == "youtube_ingest":
            title = project.youtube_title or _("(no title)")
            message = _("Video '%s' ingested. Source S3 URL set.") % title
        elif job_type in ("render", "preview"):
            message = _("Project: %s") % (project.name or "")
        elif job_type == "export":
            message = job.output_s3_url or (project.name or "")
        elif job_type == "prompt_qc":
            message = _("Quality: %s (score: %s)") % (
                project.qc_quality or _("(unknown)"),
                project.qc_score or 0,
            )
        else:
            message = project.name or ""
        return {
            "type": "success",
            "title": _("%s complete") % label,
            "message": message,
            "sticky": False,
        }
    if status == "failed":
        return {
            "type": "danger",
            "title": _("%s failed (#%s)") % (label, job.id),
            "message": job.error_message or _("Job failed without a message."),
            "sticky": True,
        }
    if status == "cancelled":
        return {
            "type": "warning",
            "title": _("%s cancelled (#%s)") % (label, job.id),
            "message": _("Job cancelled."),
            "sticky": False,
        }
    return None


def submit_job_async(db, uid, job_id, runner):
    if not _semaphore.acquire(blocking=False):
        return False
    job_id = int(job_id)
    _register_cancel_event(job_id)
    _executor.submit(_safe_worker(runner), db, uid or SUPERUSER_ID, job_id)
    return True


def in_worker_env(db, uid, callback):
    with _open_cursor(db) as cr:
        env = api.Environment(cr, uid or SUPERUSER_ID, {})
        try:
            result = callback(env, cr)
            cr.commit()
            return result
        except Exception:
            cr.rollback()
            raise


def cancellable_sleep(event, seconds):
    if event is None:
        time.sleep(seconds)
        return
    if event.wait(seconds):
        raise JobCancelled()
