import hashlib
import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)
_HEADER = "X-Video-Pipeline-Token"


def _verify_signature(raw_body, header_value, token):
    if not token or not header_value:
        return False
    expected = hmac.new(token.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value)


def _read_token():
    icp = request.env["ir.config_parameter"].sudo()
    return icp.get_param("video_editor_s3.lambda_webhook_token") or ""


class VideoPipelineCallbacks(http.Controller):

    @http.route(
        "/video_editor_s3/callback/render",
        type="http", auth="public", csrf=False, methods=["POST"],
    )
    def render_callback(self, **_kw):
        raw = request.httprequest.get_data() or b""
        header = request.httprequest.headers.get(_HEADER, "")
        token = _read_token()
        if not _verify_signature(raw, header, token):
            _logger.warning("lambda render callback: signature mismatch")
            return http.Response("forbidden", status=403)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            return http.Response("bad json", status=400)

        job_id = payload.get("job_id")
        if not job_id:
            return http.Response("missing job_id", status=400)
        env = request.env(user=1)
        job = env["video.editor.job"].sudo().browse(int(job_id))
        if not job.exists():
            return http.Response("unknown job", status=404)
        from odoo import fields as odoo_fields
        project = job.project_id
        lambda_request_id = payload.get("lambda_request_id") or ""
        status = payload.get("status") or ""

        if status == "ok":
            width = int(payload.get("width") or 0)
            height = int(payload.get("height") or 0)
            fps = float(payload.get("fps") or 0.0)
            s3_url = payload.get("s3_url") or ""
            project.write({
                "output_s3_url": s3_url,
                "edited_file_path": payload.get("s3_key") or "",
                "state": "exported",
                "edited_resolution": payload.get("resolution") or "",
                "edited_fps": fps,
            })
            job.write({
                "status": "done",
                "output_s3_url": s3_url,
                "lambda_request_id": lambda_request_id,
                "ffmpeg_command": payload.get("ffmpeg_command") or "",
                "finished_at": odoo_fields.Datetime.now(),
            })
        else:
            job.write({
                "status": "failed",
                "lambda_request_id": lambda_request_id,
                "error_message": (payload.get("error") or "lambda render failed")[:2000],
                "finished_at": odoo_fields.Datetime.now(),
            })

        env.cr.commit()
        return http.Response("ok", status=200)
