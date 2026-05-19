# -*- coding: utf-8 -*-
import io
import logging
import re
import zipfile

from odoo import http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request

_logger = logging.getLogger(__name__)

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-]+")
_ZIP_S3_CHUNK = 1 << 20
_ZIP_BATCH_SIZE = 50


def _safe_filename(name, fallback="video"):
    safe = _SAFE_FILENAME_RE.sub("_", (name or "").strip()) or fallback
    return safe[:120]


class CrowleyAIVidGenController(http.Controller):

    @http.route(
        "/crowley/seedance/video/<int:job_id>/download",
        type="http", auth="user", methods=["GET"],
    )
    def download_video(self, job_id, **kwargs):
        env = request.env
        Job = env["crowley.ai.vid.gen.job"]
        try:
            job = Job.browse(job_id)
            job.check_access_rights("read")
            job.check_access_rule("read")
        except (AccessError, MissingError):
            return request.not_found()
        if not job.exists() or job.state != "ready" or not job.s3_key:
            return request.not_found()
        ttl = int(env["ir.config_parameter"].sudo().get_param(
            "crowley_ai_vid_gen.presigned_ttl_seconds", "300"))
        storage = env["crowley.ai.vid.gen.s3.storage"].sudo()
        try:
            url = storage.presigned_get_url(
                job.sudo().s3_key,
                expires_in=ttl,
                mimetype=job.sudo().mimetype or "video/mp4",
                disposition="attachment",
                filename=f"{job.sudo().name or 'video'}.mp4",
            )
        except Exception:
            _logger.exception("Failed to generate download URL for job %s", job_id)
            return request.not_found()
        return request.redirect(url, code=302, local=False)

    @http.route(
        "/crowley/seedance/zip",
        type="http", auth="user", methods=["GET"],
    )
    def download_zip(self, **kwargs):
        env = request.env
        Job = env["crowley.ai.vid.gen.job"]
        try:
            jobs = Job.search([], order="create_date desc")
        except AccessError:
            return request.not_found()

        ready_attempts = jobs.attempt_ids.filtered(
            lambda a: a.state == "ready" and a.s3_key
        ).sorted(key=lambda a: (a.job_id.create_date, a.attempt_number), reverse=True)
        if not ready_attempts:
            return request.not_found()

        storage = env["crowley.ai.vid.gen.s3.storage"].sudo()
        s3 = storage._client()

        def _stream():
            buf = io.BytesIO()
            with zipfile.ZipFile(
                buf, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True,
            ) as zf:
                seen = {}
                for attempt in ready_attempts.sudo():
                    base = _safe_filename(attempt.job_id.name) or f"job-{attempt.job_id.id}"
                    candidate = f"{base}-a{attempt.attempt_number}.mp4"
                    if candidate in seen:
                        seen[candidate] += 1
                        candidate = f"{base}-a{attempt.attempt_number}-{seen[candidate]}.mp4"
                    else:
                        seen[candidate] = 0
                    try:
                        obj = s3.get_object(Bucket=attempt.s3_bucket, Key=attempt.s3_key)
                    except Exception:
                        _logger.exception(
                            "ZIP: failed to fetch S3 object for attempt %s", attempt.id,
                        )
                        continue
                    body = obj["Body"]
                    info = zipfile.ZipInfo(candidate)
                    info.compress_type = zipfile.ZIP_STORED
                    with zf.open(info, mode="w", force_zip64=True) as zentry:
                        while True:
                            chunk = body.read(_ZIP_S3_CHUNK)
                            if not chunk:
                                break
                            zentry.write(chunk)
                            if buf.tell() >= _ZIP_S3_CHUNK:
                                data = buf.getvalue()
                                buf.seek(0)
                                buf.truncate(0)
                                yield data
                    if buf.tell() >= _ZIP_S3_CHUNK:
                        data = buf.getvalue()
                        buf.seek(0)
                        buf.truncate(0)
                        yield data
            tail = buf.getvalue()
            if tail:
                yield tail

        headers = [
            ("Content-Type", "application/zip"),
            ("Content-Disposition", 'attachment; filename="seedance-history.zip"'),
            ("Cache-Control", "private, no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        return request.make_response(_stream(), headers=headers)
