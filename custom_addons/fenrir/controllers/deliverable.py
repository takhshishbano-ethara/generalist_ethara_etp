"""HTTP endpoints for seller deliverable uploads.

The upload endpoint streams the multipart body to S3 via boto3 (using
``upload_fileobj`` so big files use multipart and don't materialize in
memory). The bytes never touch Odoo's ir.attachment / filestore.
"""
import logging
import mimetypes

from odoo import fields, http
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.http import request


_logger = logging.getLogger(__name__)


class FenrirDeliverableController(http.Controller):

    @http.route(
        "/fenrir/seller-offer/<int:offer_id>/deliverable/upload",
        type="http", auth="user", methods=["POST"], csrf=False)
    def upload(self, offer_id, **kwargs):
        Offer = request.env["fenrir.seller.offer"]
        offer = Offer.browse(offer_id).exists()
        if not offer:
            return request.make_json_response(
                {"error": "offer not found"}, status=404)
        try:
            offer.check_access("write")
        except AccessError:
            return request.make_json_response(
                {"error": "access denied"}, status=403)

        file_storage = request.httprequest.files.get("file")
        if not file_storage:
            return request.make_json_response(
                {"error": "no file in request"}, status=400)

        file_name = file_storage.filename or "deliverable"
        mime = (file_storage.mimetype
                or mimetypes.guess_type(file_name)[0]
                or "application/octet-stream")

        # Create the DB row first so we have an id for the S3 key.
        deliv = request.env["fenrir.seller.deliverable"].create({
            "offer_id": offer.id,
            "file_name": file_name,
            "mime_type": mime,
        })
        key = deliv.build_s3_key()

        s3 = request.env["fenrir.s3.service"]
        client, bucket, _f = s3._build_client()
        stream = file_storage.stream  # werkzeug SpooledTemporaryFile / BytesIO
        try:
            client.upload_fileobj(
                stream, bucket, key,
                ExtraArgs={"ContentType": mime})
            head = client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                "Fenrir: S3 upload failed for deliverable %s", deliv.id)
            deliv.unlink()
            return request.make_json_response(
                {"error": f"S3 upload failed: {exc}"}, status=502)

        deliv.write({
            "s3_key": key,
            "s3_etag": (head.get("ETag") or "").strip('"'),
            "file_size": head.get("ContentLength", 0),
            "s3_uploaded_at": fields.Datetime.now(),
        })
        _logger.info(
            "Fenrir: uploaded deliverable %s to S3 at %s (%s bytes)",
            deliv.id, key, deliv.file_size)

        return request.make_json_response({
            "id": deliv.id,
            "offer_id": offer.id,
            "file_name": deliv.file_name,
            "mime_type": deliv.mime_type,
            "file_size": deliv.file_size,
            "s3_key": deliv.s3_key,
            "s3_uploaded_at": deliv.s3_uploaded_at and str(deliv.s3_uploaded_at),
        })

    @http.route(
        "/fenrir/deliverable/<int:deliverable_id>/download",
        type="http", auth="user", methods=["GET"])
    def download(self, deliverable_id, **kwargs):
        deliv = request.env["fenrir.seller.deliverable"].browse(
            deliverable_id).exists()
        if not deliv:
            raise MissingError("Deliverable not found.")
        deliv.check_access("read")
        if not deliv.s3_key:
            raise UserError("This deliverable has no S3 object yet.")
        config = request.env["fenrir.drive.config"].sudo().get_singleton()
        days = config.s3_presigned_url_expiry_days or 7
        url = request.env["fenrir.s3.service"].presigned_get_url(
            deliv.s3_key, days * 24 * 3600)
        return request.redirect(url, code=302, local=False)

    @http.route(
        "/fenrir/deliverable/<int:deliverable_id>/delete",
        type="http", auth="user", methods=["POST"], csrf=False)
    def delete(self, deliverable_id, **kwargs):
        deliv = request.env["fenrir.seller.deliverable"].browse(
            deliverable_id).exists()
        if not deliv:
            return request.make_json_response(
                {"error": "not found"}, status=404)
        try:
            deliv.check_access("unlink")
        except AccessError:
            return request.make_json_response(
                {"error": "access denied"}, status=403)
        deliv.unlink()
        return request.make_json_response({"deleted": deliverable_id})
