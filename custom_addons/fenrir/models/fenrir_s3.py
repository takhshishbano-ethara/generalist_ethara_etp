"""AWS S3 service for Fenrir task uploads.

Big uploaded attachments go to S3 (boto3 handles multipart for large files
automatically), while small generated text files continue to land in Drive.

Credentials live on fenrir.drive.config.
"""

import io
import logging

from odoo import _, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class FenrirS3Service(models.AbstractModel):
    _name = "fenrir.s3.service"
    _description = "Fenrir — AWS S3 Upload Service"

    # ── Config + client ──────────────────────────────────────────────────
    def _build_client(self):
        """Return (s3_client, bucket, folder_prefix)."""
        config = self.env["fenrir.drive.config"].sudo().get_singleton()
        bucket = (config.s3_bucket or "").strip()
        if not bucket:
            raise UserError(_(
                "S3 bucket is not configured.\n"
                "Set it under Fenrir → Configuration → Google Drive."))
        access_key = (config.s3_access_key_id or "").strip()
        secret = (config.s3_secret_access_key or "").strip()
        if not (access_key and secret):
            raise UserError(_(
                "S3 access key / secret missing.\n"
                "Set them under Fenrir → Configuration → Google Drive."))
        region = (config.s3_region or "us-east-1").strip()
        endpoint = (config.s3_endpoint_url or "").strip() or None
        folder = (config.s3_folder or "").strip().strip("/")

        try:
            import boto3
        except ImportError as exc:
            raise UserError(_(
                "Python package 'boto3' is not installed.\n"
                "Run:  pip install boto3"
            )) from exc

        client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret,
            endpoint_url=endpoint,
        )
        return client, bucket, folder

    # ── Helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _join_key(*parts):
        return "/".join(p.strip("/") for p in parts if p)

    def _task_prefix(self, task):
        """S3 key prefix for one task: <folder>/<TASK_CODE>/ ."""
        _client, _bucket, folder = self._build_client()
        return self._join_key(folder, task.code or f"task_{task.id}") + "/"

    # ── Operations ───────────────────────────────────────────────────────
    def upload_bytes(self, key, content, mime="application/octet-stream"):
        """Upload one object. Uses upload_fileobj so big payloads stream + multipart."""
        client, bucket, _folder = self._build_client()
        client.upload_fileobj(
            io.BytesIO(content), bucket, key,
            ExtraArgs={"ContentType": mime})
        return key

    def download_bytes(self, key):
        """Download one object's bytes. Used to re-hydrate attachments whose
        local copy was cleared after the attach-time S3 push."""
        client, bucket, _folder = self._build_client()
        buf = io.BytesIO()
        client.download_fileobj(bucket, key, buf)
        return buf.getvalue()

    def delete_prefix(self, prefix):
        """Delete every object under the given key prefix (used on re-upload)."""
        client, bucket, _folder = self._build_client()
        paginator = client.get_paginator("list_objects_v2")
        keys_to_delete = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                keys_to_delete.append({"Key": obj["Key"]})
                # Delete in batches of 1000 (S3 limit)
                if len(keys_to_delete) >= 1000:
                    client.delete_objects(
                        Bucket=bucket, Delete={"Objects": keys_to_delete})
                    keys_to_delete = []
        if keys_to_delete:
            client.delete_objects(
                Bucket=bucket, Delete={"Objects": keys_to_delete})

    def presigned_get_url(self, key, expiry_seconds):
        client, bucket, _folder = self._build_client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=int(expiry_seconds))
