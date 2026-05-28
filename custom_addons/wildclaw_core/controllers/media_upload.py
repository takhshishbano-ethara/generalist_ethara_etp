import base64
import json
import logging

from odoo import http
from odoo.http import request

from ..services import media_processor

_logger = logging.getLogger(__name__)


class WildclawMediaUpload(http.Controller):

    @http.route("/wildclaw_core/media/upload", type="http", auth="user", methods=["POST"], csrf=False)
    def upload(self, **post):
        upload = post.get("file")
        if not upload:
            return request.make_response(json.dumps({"error": "no file"}), headers=[("Content-Type", "application/json")])
        file_bytes = upload.read()
        max_mb = int(request.env["ir.config_parameter"].sudo().get_param("wildclaw.media_max_upload_mb", "100"))
        if len(file_bytes) > max_mb * 1024 * 1024:
            return request.make_response(
                json.dumps({"error": f"file too large (>{max_mb}MB)"}),
                headers=[("Content-Type", "application/json")],
                status=413,
            )
        rec = media_processor.process_upload(
            request.env,
            file_bytes=file_bytes,
            filename=upload.filename,
            mime_type=upload.content_type or "application/octet-stream",
            sandbox_model=post.get("sandbox_model"),
            sandbox_id_int=int(post.get("sandbox_id") or 0) or None,
            task_id_str=post.get("task_id_str"),
        )
        return request.make_response(
            json.dumps({
                "id": rec.id,
                "name": rec.name,
                "mime_type": rec.mime_type,
                "media_kind": rec.media_kind,
                "byte_size": rec.byte_size,
                "s3_url": rec.s3_url,
                "sha256_hex": rec.sha256_hex,
            }),
            headers=[("Content-Type", "application/json")],
        )

    @http.route("/wildclaw_core/media/<int:attachment_id>", type="json", auth="user", methods=["POST"])
    def info(self, attachment_id, **kwargs):
        rec = request.env["wildclaw.media.attachment"].browse(int(attachment_id)).exists()
        if not rec:
            return {"error": "not found"}
        return {
            "id": rec.id,
            "name": rec.name,
            "mime_type": rec.mime_type,
            "media_kind": rec.media_kind,
            "s3_url": rec.s3_url,
            "image_width": rec.image_width,
            "image_height": rec.image_height,
            "video_duration_s": rec.video_duration_s,
            "frame_extract_count": rec.frame_extract_count,
            "pdf_page_count": rec.pdf_page_count,
        }
