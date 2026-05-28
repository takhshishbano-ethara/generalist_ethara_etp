# -*- coding: utf-8 -*-
import atexit
import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from odoo import SUPERUSER_ID, api
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

_MAX_WORKER_THREADS = int(os.environ.get("VIDEO_EDITOR_S3_MAX_WORKERS", "2") or 2)
_MAX_CONCURRENT_JOBS = int(os.environ.get("VIDEO_EDITOR_S3_MAX_CONCURRENT", "2") or 2)
_HEARTBEAT_INTERVAL_SECONDS = 10
_HEARTBEAT_STALE_SECONDS = 120
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
