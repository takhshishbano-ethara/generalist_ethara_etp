# -*- coding: utf-8 -*-
import json
import logging
import os

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..services import s3_storage

try:
    from werkzeug.utils import send_file as _werkzeug_send_file
except ImportError:
    from odoo.tools._vendor.send_file import send_file as _werkzeug_send_file

_logger = logging.getLogger(__name__)

_STREAM_KIND_FIELD = {
    "edited": "edited_file_path",
    "preview": "preview_file_path",
}
_STREAM_KINDS = ("source", "edited", "preview")


def _json_response(data, status=200):
    body = json.dumps(data, default=str)
    return request.make_response(
        body,
        status=status,
        headers=[("Content-Type", "application/json")],
    )


def _json_error(message, status=400, code="error"):
    return _json_response({"error": code, "message": str(message)}, status=status)


def _read_payload():
    payload = None
    raw = request.httprequest.data
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
    if not isinstance(payload, dict):
        payload = {}
    params = payload.get("params") if "params" in payload else payload
    return params if isinstance(params, dict) else {}


def _get_project(project_id, mode="read"):
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        raise UserError("Invalid project id")
    project = request.env["video.editor.project"].browse(pid).exists()
    if not project:
        raise UserError("Project not found")
    project.check_access(mode)
    return project


def _get_job(job_id, mode="read"):
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        raise UserError("Invalid job id")
    job = request.env["video.editor.job"].browse(jid).exists()
    if not job:
        raise UserError("Job not found")
    job.check_access(mode)
    return job


def _source_playable(project):
    url = (project.s3_source_url or "").strip()
    if not url:
        return False
    if url.startswith(("http://", "https://")) and "://" in url:
        rest = url.split("://", 1)[1]
        if "/" in rest and rest.split("/", 1)[0]:
            return True
    bucket, key = s3_storage.parse_s3_url(url)
    if not bucket or not key:
        return False
    cfg = request.env["video.editor.s3.settings"].sudo().get_s3_config()
    return bool(cfg.get("access_key") and cfg.get("secret_key"))


def _project_payload(project):
    active_job = project.active_job_id
    return {
        "id": project.id,
        "name": project.name,
        "state": project.state,
        "s3_source_url": project.s3_source_url,
        "s3_source_key": project.s3_source_key,
        "source_metadata": project.source_metadata or {},
        "duration_seconds": project.duration_seconds,
        "resolution": project.resolution,
        "source_size_mb": project.source_size_mb,
        "editing_config": project.editing_config or {},
        "has_source": _source_playable(project),
        "has_edited": bool(project.edited_file_path),
        "has_preview": bool(project.preview_file_path),
        "output_s3_url": project.output_s3_url,
        "active_job_id": active_job.id if active_job else False,
    }


def _job_payload(job):
    return {
        "id": job.id,
        "project_id": job.project_id.id,
        "job_type": job.job_type,
        "status": job.status,
        "progress_text": job.progress_text or "",
        "last_heartbeat": job.last_heartbeat and job.last_heartbeat.isoformat() or None,
        "started_at": job.started_at and job.started_at.isoformat() or None,
        "finished_at": job.finished_at and job.finished_at.isoformat() or None,
        "duration_ms": job.duration_ms,
        "output_path": job.output_path,
        "output_s3_url": job.output_s3_url,
        "error_message": job.error_message or "",
    }


def _stream_url(project_id, kind):
    return f"/video_editor/stream/{project_id}/{kind}"


def _redirect_to_source(project):
    url = (project.s3_source_url or "").strip()
    if not url:
        return request.not_found()
    bucket, key = s3_storage.parse_s3_url(url)
    cfg = request.env["video.editor.s3.settings"].sudo().get_s3_config()
    if bucket and key and cfg.get("access_key") and cfg.get("secret_key"):
        try:
            target = s3_storage.generate_presigned_url(
                {**cfg, "bucket": bucket}, key, expires_in=3600,
            )
            return request.redirect(target, code=302, local=False)
        except Exception as exc:
            _logger.warning("presign failed for project %s: %s", project.id, exc)
    if url.startswith(("http://", "https://")):
        return request.redirect(url, code=302, local=False)
    return request.not_found()


class VideoEditorS3Controller(http.Controller):

    @http.route("/video_editor/load", type="http", auth="user", methods=["POST"], csrf=False)
    def load(self, **_kwargs):
        try:
            params = _read_payload()
            s3_url = (params.get("s3_url") or "").strip()
            project_id = params.get("project_id")
            name = params.get("name")
            if project_id:
                project = _get_project(project_id, mode="write")
                if s3_url and s3_url != project.s3_source_url:
                    project.write({"s3_source_url": s3_url, "source_metadata": False, "state": "draft"})
            else:
                if not s3_url:
                    return _json_error("s3_url is required", status=400, code="missing_s3_url")
                vals = {"s3_source_url": s3_url}
                if name:
                    vals["name"] = name
                project = request.env["video.editor.project"].create(vals)
            payload = _project_payload(project)
            payload["stream_url"] = _stream_url(project.id, "source")
            return _json_response({"result": payload})
        except (AccessError, UserError, ValidationError) as exc:
            return _json_error(exc, status=400, code="user_error")
        except Exception as exc:
            _logger.exception("video_editor_s3 load failed")
            return _json_error(exc, status=500, code="server_error")

    @http.route("/video_editor/process", type="http", auth="user", methods=["POST"], csrf=False)
    def process(self, **_kwargs):
        try:
            params = _read_payload()
            project = _get_project(params.get("project_id"), mode="write")
            config = params.get("config") or {}
            if not isinstance(config, dict):
                return _json_error("config must be an object", status=400, code="invalid_config")
            preview = bool(params.get("preview"))
            if preview:
                job = project.action_preview(config=config)
            else:
                job = project.action_render(config=config)
            return _json_response({"result": _job_payload(job)})
        except (AccessError, UserError, ValidationError) as exc:
            return _json_error(exc, status=400, code="user_error")
        except Exception as exc:
            _logger.exception("video_editor_s3 process failed")
            return _json_error(exc, status=500, code="server_error")

    @http.route("/video_editor/export", type="http", auth="user", methods=["POST"], csrf=False)
    def export(self, **_kwargs):
        try:
            params = _read_payload()
            project = _get_project(params.get("project_id"), mode="write")
            s3_key = params.get("s3_key") or None
            job = project.action_export(s3_key=s3_key)
            return _json_response({"result": _job_payload(job)})
        except (AccessError, UserError, ValidationError) as exc:
            return _json_error(exc, status=400, code="user_error")
        except Exception as exc:
            _logger.exception("video_editor_s3 export failed")
            return _json_error(exc, status=500, code="server_error")

    @http.route("/video_editor/status/<int:job_id>", type="http", auth="user", methods=["GET"], csrf=False)
    def status(self, job_id, **_kwargs):
        try:
            job = _get_job(job_id, mode="read")
            return _json_response({"result": _job_payload(job)})
        except (AccessError, UserError, ValidationError) as exc:
            return _json_error(exc, status=404, code="not_found")
        except Exception as exc:
            _logger.exception("video_editor_s3 status failed")
            return _json_error(exc, status=500, code="server_error")

    @http.route("/video_editor/project/<int:project_id>", type="http", auth="user", methods=["GET"], csrf=False)
    def project_state(self, project_id, **_kwargs):
        try:
            project = _get_project(project_id, mode="read")
            payload = _project_payload(project)
            payload["stream_url"] = _stream_url(project.id, "source")
            if project.edited_file_path:
                payload["edited_stream_url"] = _stream_url(project.id, "edited")
            if project.preview_file_path:
                payload["preview_stream_url"] = _stream_url(project.id, "preview")
            return _json_response({"result": payload})
        except (AccessError, UserError, ValidationError) as exc:
            return _json_error(exc, status=404, code="not_found")
        except Exception as exc:
            _logger.exception("video_editor_s3 project_state failed")
            return _json_error(exc, status=500, code="server_error")

    @http.route(
        "/video_editor/stream/<int:project_id>/<string:kind>",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def stream(self, project_id, kind, **_kwargs):
        if kind not in _STREAM_KINDS:
            return request.not_found()
        try:
            project = _get_project(project_id, mode="read")
        except (AccessError, UserError):
            return request.not_found()
        if kind == "source":
            return _redirect_to_source(project)
        rel_path = project[_STREAM_KIND_FIELD[kind]]
        if not rel_path:
            return request.not_found()
        try:
            abs_path = request.env["video.editor.s3.media.storage"].absolute(rel_path)
        except UserError:
            return request.not_found()
        if not os.path.isfile(abs_path):
            return request.not_found()
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError:
            mtime = None
        response = _werkzeug_send_file(
            abs_path,
            request.httprequest.environ,
            mimetype="video/mp4",
            conditional=True,
            etag=True,
            last_modified=mtime,
            download_name=os.path.basename(abs_path),
        )
        return response

    @http.route("/video_editor/ingest_youtube", type="http", auth="user", methods=["POST"], csrf=False)
    def ingest_youtube(self, **_kwargs):
        try:
            params = _read_payload()
            project = _get_project(params.get("project_id"), mode="write")
            youtube_url = (params.get("youtube_url") or "").strip()
            if youtube_url and youtube_url != project.youtube_url:
                project.youtube_url = youtube_url
            job = project.action_ingest_youtube()
            return _json_response({"result": _job_payload(job)})
        except (AccessError, UserError, ValidationError) as exc:
            return _json_error(exc, status=400, code="user_error")
        except Exception as exc:
            _logger.exception("video_editor_s3 ingest_youtube failed")
            return _json_error(exc, status=500, code="server_error")

    @http.route("/video_editor/cancel/<int:job_id>", type="http", auth="user", methods=["POST"], csrf=False)
    def cancel(self, job_id, **_kwargs):
        try:
            job = _get_job(job_id, mode="write")
            job.action_cancel()
            return _json_response({"result": _job_payload(job)})
        except (AccessError, UserError, ValidationError) as exc:
            return _json_error(exc, status=400, code="user_error")
        except Exception as exc:
            _logger.exception("video_editor_s3 cancel failed")
            return _json_error(exc, status=500, code="server_error")
