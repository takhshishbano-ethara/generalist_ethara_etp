"""HTTP endpoints for direct-to-S3 task attachment uploads.

Mirrors the seller-deliverable controller: a multipart body is streamed
to S3 via boto3.upload_fileobj (multipart for big files) and a
fenrir.task.attachment row is created with the s3_key already set, so
the model's post-create hook stays out of the way and no bytes ever
land in Odoo's filestore.
"""
import logging
import mimetypes

from odoo import fields, http
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.http import request


_logger = logging.getLogger(__name__)

_ALLOWED_FOLDERS = {"root", "resources", "tests", "environment", "data"}


class FenrirTaskAttachmentController(http.Controller):

    @http.route(
        "/fenrir/task/<int:task_id>/attachment/upload",
        type="http", auth="user", methods=["POST"], csrf=False)
    def upload(self, task_id, **kwargs):
        Task = request.env["fenrir.task"]
        task = Task.browse(task_id).exists()
        if not task:
            return request.make_json_response(
                {"error": "task not found"}, status=404)
        try:
            task.check_access("write")
        except AccessError:
            return request.make_json_response(
                {"error": "access denied"}, status=403)

        file_storage = request.httprequest.files.get("file")
        if not file_storage:
            return request.make_json_response(
                {"error": "no file in request"}, status=400)

        folder = (kwargs.get("folder") or "resources").strip()
        if folder not in _ALLOWED_FOLDERS:
            folder = "resources"

        file_name = file_storage.filename or "attachment"
        mime = (file_storage.mimetype
                or mimetypes.guess_type(file_name)[0]
                or "application/octet-stream")

        # Create the row first so we can derive a deterministic S3 key.
        Attachment = request.env["fenrir.task.attachment"]
        att = Attachment.with_context(fenrir_skip_s3_push=True).create({
            "task_id": task.id,
            "file_name": file_name,
            "folder": folder,
            "license": "self_created",
            "is_generated": False,
        })
        key = att._compute_s3_key()

        s3 = request.env["fenrir.s3.service"]
        client, bucket, _f = s3._build_client()
        stream = file_storage.stream
        try:
            client.upload_fileobj(
                stream, bucket, key,
                ExtraArgs={"ContentType": mime})
            head = client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                "Fenrir: S3 upload failed for task attachment %s", att.id)
            att.unlink()
            return request.make_json_response(
                {"error": f"S3 upload failed: {exc}"}, status=502)

        att.with_context(fenrir_skip_s3_push=True).write({
            "s3_key": key,
            "s3_uploaded_at": fields.Datetime.now(),
            "file_size": head.get("ContentLength", 0),
        })
        _logger.info(
            "Fenrir: uploaded task-attachment %s to S3 at %s (%s bytes)",
            att.id, key, att.file_size)

        return request.make_json_response({
            "id": att.id,
            "task_id": task.id,
            "file_name": att.file_name,
            "folder": att.folder,
            "file_size": att.file_size,
            "s3_key": att.s3_key,
            "s3_uploaded_at": att.s3_uploaded_at and str(att.s3_uploaded_at),
        })
