# -*- coding: utf-8 -*-
import json
import logging

from odoo import fields as odoo_fields, http
from odoo.http import request

from ..services import job_executor

_logger = logging.getLogger(__name__)


class YoutubeEc2Callbacks(http.Controller):

    @http.route(
        "/video_editor_s3/callback/youtube_ec2",
        type="http", auth="public", csrf=False, methods=["POST"],
    )
    def youtube_ec2_callback(self, **_kw):
        raw = request.httprequest.get_data() or b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            return http.Response("bad json", status=400)

        tasker_id = payload.get("tasker_id")
        if tasker_id is None or tasker_id == "":
            return http.Response("missing tasker_id", status=400)
        try:
            job_id = int(tasker_id)
        except (TypeError, ValueError):
            return http.Response("invalid tasker_id", status=400)

        env = request.env(user=1)
        job = env["video.editor.job"].sudo().browse(job_id)
        if not job.exists():
            return http.Response("unknown job", status=404)

        if job.status in ("done", "failed", "cancelled"):
            _logger.info(
                "youtube_ec2 callback: job %s already %s; ignoring duplicate",
                job_id, job.status,
            )
            return http.Response("ok", status=200)

        project = job.project_id
        status = (payload.get("status") or "").lower()
        ec2_job_id = str(payload.get("job_id") or "")

        if status == "completed":
            s3_url = payload.get("s3_url") or ""
            opts = job.config_json or {}
            start_seconds = float(opts.get("start_seconds") or 0.0)
            end_seconds = float(opts.get("end_seconds") or 0.0)
            is_clip = start_seconds > 0.0 or end_seconds > 0.0
            project_vals = (
                {"output_s3_url": s3_url}
                if is_clip
                else {"s3_source_url": s3_url}
            )
            project_vals["youtube_ingested_at"] = odoo_fields.Datetime.now()
            project.write(project_vals)
            job.write({
                "status": "done",
                "output_s3_url": s3_url,
                "finished_at": odoo_fields.Datetime.now(),
                "progress_text": ("EC2 callback: completed (ec2_job=%s)" % ec2_job_id[:32])[:255],
            })
        else:
            err = (
                payload.get("error")
                or payload.get("error_message")
                or payload.get("message")
                or ("EC2 ingest %s" % (status or "unknown"))
            )
            job.write({
                "status": "failed",
                "error_message": str(err)[:2000],
                "finished_at": odoo_fields.Datetime.now(),
                "progress_text": ("EC2 callback: %s" % (status or "error"))[:255],
            })

        env.cr.commit()
        try:
            job_executor._notify_job_completion(env.cr.dbname, job_id)
        except Exception:
            _logger.exception("youtube_ec2 callback: failed to fire bus notification for job %s", job_id)
        return http.Response("ok", status=200)
