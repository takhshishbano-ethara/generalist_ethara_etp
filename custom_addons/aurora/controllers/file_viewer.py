"""HTTP endpoints for streaming evaluation artifacts.

Two modes per log file:
- During a run: the local path on the worker is still valid; tail it from disk.
- After a run: the S3 URI is populated; download on demand (or redirect via
  presigned URL if S3 has credentials but the object is private).

The ?offset=N query parameter enables polling (fetch only new bytes).
"""
import logging
import os

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_LOG_KINDS = {
    "build_log": ("build_log_local_path", "build_log_s3_uri"),
    "run_log": ("run_log_local_path", "run_log_s3_uri"),
    "test_patch_log": ("test_patch_log_local_path", "test_patch_log_s3_uri"),
    "fix_patch_log": ("fix_patch_log_local_path", "fix_patch_log_s3_uri"),
}

_MAX_TAIL_BYTES = 5 * 1024 * 1024


class AuroraEvaluationInstanceController(http.Controller):

    @http.route(
        "/aurora/evaluation/instance/<int:instance_id>/log/<string:kind>",
        type="http", auth="user", methods=["GET"],
    )
    def download_log(self, instance_id, kind, offset=None, **kwargs):
        if kind not in _LOG_KINDS:
            return request.not_found()
        rec = request.env["aurora.evaluation.instance"].browse(instance_id)
        rec.check_access_rights("read")
        rec.check_access_rule("read")
        if not rec.exists():
            return request.not_found()

        local_field, s3_field = _LOG_KINDS[kind]
        local_path = rec[local_field]
        s3_uri = rec[s3_field]

        try:
            offset_int = int(offset) if offset else 0
        except (TypeError, ValueError):
            offset_int = 0

        if local_path and os.path.isfile(local_path):
            try:
                size = os.path.getsize(local_path)
                if offset_int < 0 or offset_int > size:
                    offset_int = max(0, size - _MAX_TAIL_BYTES)
                with open(local_path, "rb") as f:
                    f.seek(offset_int)
                    payload = f.read(_MAX_TAIL_BYTES)
                headers = [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("X-Aurora-Source", "local"),
                    ("X-Aurora-Offset", str(offset_int)),
                    ("X-Aurora-Next-Offset", str(offset_int + len(payload))),
                    ("X-Aurora-Total-Size", str(size)),
                ]
                return request.make_response(payload, headers=headers)
            except OSError:
                _logger.warning(
                    "Failed to read local log %s for instance %s",
                    local_path, instance_id, exc_info=True,
                )

        if s3_uri:
            return request.redirect(s3_uri, local=False)

        return request.not_found()
