"""Seller offer deliverable file — stored in S3 only.

This model exists because Odoo's stock binary widget routes uploads through
ir.attachment, which means bytes land in the local filestore. For seller
deliverables we want a direct browser → controller → S3 pipeline with the
local Odoo filestore staying clean. The record itself stores only metadata
plus the S3 object key.

Files live under a FLAT S3 prefix (<s3_folder>/deliverables/<id>_<name>),
intentionally different from the Drive-export layout which is hierarchical.
"""
import logging
import re

from odoo import _, api, fields, models


_logger = logging.getLogger(__name__)


def _slug(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "").strip("_") or "file"


class FenrirSellerDeliverable(models.Model):
    _name = "fenrir.seller.deliverable"
    _description = "Fenrir Seller Deliverable (S3-backed)"
    _order = "offer_id, id"
    _rec_name = "file_name"

    offer_id = fields.Many2one(
        comodel_name="fenrir.seller.offer",
        string="Seller Offer",
        required=True,
        ondelete="cascade",
        index=True,
    )
    task_id = fields.Many2one(
        comodel_name="fenrir.task",
        string="Task",
        related="offer_id.task_id",
        store=True,
        readonly=True,
    )
    file_name = fields.Char(string="File Name", required=True)
    mime_type = fields.Char(string="MIME Type", default="application/octet-stream")
    file_size = fields.Integer(string="File Size (bytes)", readonly=True)
    s3_key = fields.Char(string="S3 Key", readonly=True, copy=False)
    s3_etag = fields.Char(string="S3 ETag", readonly=True, copy=False)
    s3_uploaded_at = fields.Datetime(
        string="S3 Uploaded At", readonly=True, copy=False)

    def build_s3_key(self):
        """Flat S3 key. The model row must already exist (we use its id)."""
        self.ensure_one()
        config = self.env["fenrir.drive.config"].sudo().get_singleton()
        folder = (config.s3_folder or "").strip().strip("/")
        safe = _slug(self.file_name or f"deliverable_{self.id}")
        parts = [folder, "deliverables", f"{self.id}_{safe}"]
        return "/".join(p for p in parts if p)

    def action_open_s3_download(self):
        self.ensure_one()
        if not self.s3_key:
            return False
        config = self.env["fenrir.drive.config"].sudo().get_singleton()
        days = config.s3_presigned_url_expiry_days or 7
        url = self.env["fenrir.s3.service"].presigned_get_url(
            self.s3_key, days * 24 * 3600)
        return {"type": "ir.actions.act_url", "url": url, "target": "new"}

    def fetch_bytes(self):
        self.ensure_one()
        if not self.s3_key:
            return b""
        return self.env["fenrir.s3.service"].download_bytes(self.s3_key)

    def unlink(self):
        """Delete the S3 object along with the DB row. Best-effort —
        if S3 fails we still drop the row; orphaned objects can be cleaned
        out-of-band."""
        s3 = self.env["fenrir.s3.service"]
        to_delete = [(rec.id, rec.s3_key) for rec in self if rec.s3_key]
        result = super().unlink()
        for rec_id, key in to_delete:
            try:
                client, bucket, _f = s3._build_client()
                client.delete_object(Bucket=bucket, Key=key)
                _logger.info("Fenrir: deleted S3 object %s (deliverable %s)",
                             key, rec_id)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Fenrir: failed to delete S3 object %s for "
                    "deliverable %s: %s", key, rec_id, exc)
        return result
