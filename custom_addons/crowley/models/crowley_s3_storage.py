"""Crowley S3 storage helper.

A thin AbstractModel that talks to boto3 directly. It reads the
``s3.connector`` record for credentials (bucket name + access key + secret +
region) but goes straight to boto3 for presigned URLs / SHA-256 / explicit
multipart config — the existing ``s3.connector.upload_to_s3`` API does not
expose any of those knobs.

The bucket name lives in the ``name`` field on ``s3.connector`` (verified
against ``custom_addons/s3_connector/models/s3_connector.py``, where
``upload_file`` / ``upload_to_s3`` reference ``self.name`` as the bucket).
"""

import hashlib
import logging
import threading

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Tuned for video uploads — 16MB chunks, 4 concurrent.
TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,    # 8MB
    multipart_chunksize=16 * 1024 * 1024,   # 16MB
    max_concurrency=4,
    use_threads=True,
)


class S3StorageError(Exception):
    """Generic Crowley S3 storage error (raised back to the caller)."""


class S3VerificationError(S3StorageError):
    """Raised when verify_object_sha256 disagrees with the streaming hash."""


# Thread-safe client cache: key = (db, connector_id, secret_fingerprint, region).
# Module-level so it survives across AbstractModel invocations in a worker.
_CLIENT_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


class CrowleyS3Storage(models.AbstractModel):
    _name = "crowley.s3.storage"
    _description = "Crowley S3 storage helper (presigned URLs + SHA-256)"

    # ------------------------------------------------------------------
    # Connector + client
    # ------------------------------------------------------------------
    @api.model
    def _client_for(self, connector):
        """Return a (cached) boto3 S3 client for the given ``s3.connector``.

        Cache key includes a fingerprint of the secret key so that rotating
        credentials in the connector record automatically invalidates the
        cached client.
        """
        if not connector or not connector.exists():
            raise UserError(_("S3 connector not found."))
        secret_fp = (
            hashlib.sha256((connector.aws_secret_access_key or "").encode()).hexdigest()[:16]
            if connector.aws_secret_access_key else ""
        )
        cache_key = (
            self.env.cr.dbname,
            connector.id,
            secret_fp,
            connector.region_name or "",
        )
        client = _CLIENT_CACHE.get(cache_key)
        if client is not None:
            return client

        with _CACHE_LOCK:
            client = _CLIENT_CACHE.get(cache_key)
            if client is not None:
                return client

            cfg = BotoConfig(
                retries={"max_attempts": 5, "mode": "adaptive"},
                connect_timeout=10,
                read_timeout=120,
                max_pool_connections=20,
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
            )
            kwargs = {
                "service_name": "s3",
                "region_name": connector.region_name or None,
                "config": cfg,
            }
            if connector.aws_access_key_id and connector.aws_secret_access_key:
                kwargs["aws_access_key_id"] = connector.aws_access_key_id
                kwargs["aws_secret_access_key"] = connector.aws_secret_access_key
            # Force the regional endpoint when region != us-east-1. Without
            # this, presigned URLs against the legacy global endpoint may
            # return 307 redirects that HTML5 <video> elements refuse to
            # follow.
            region = connector.region_name or ""
            if region and region != "us-east-1":
                kwargs["endpoint_url"] = f"https://s3.{region}.amazonaws.com"
            try:
                client = boto3.client(**kwargs)
            except NoCredentialsError as e:
                raise UserError(_(
                    "S3 credentials are not available. "
                    "Configure access_key/secret_key on the s3.connector record."
                )) from e
            _CLIENT_CACHE[cache_key] = client
            _logger.info(
                "Crowley S3: client created for connector_id=%s region=%s",
                connector.id, region or "<aws-default>",
            )
            return client

    @api.model
    def _bucket_for(self, connector):
        """Return the bucket name for a connector.

        Verified in ``s3_connector/models/s3_connector.py``: the ``name``
        field holds the bucket name (line 18:
        ``name = fields.Char(string="Bucket Name", required=True)``).
        """
        if not connector or not connector.exists():
            raise UserError(_("S3 connector not found."))
        if not connector.name:
            raise UserError(_("S3 connector has no bucket name configured."))
        return connector.name

    @api.model
    def clear_cache(self):
        with _CACHE_LOCK:
            _CLIENT_CACHE.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @api.model
    def presigned_get_url(self, connector_id, object_key, *,
                          expires_in=300, mimetype="video/mp4",
                          disposition="inline", filename=None):
        """Generate a time-limited SigV4 presigned GET URL.

        :param disposition: ``inline`` (play in browser) or ``attachment``.
        :param filename: optional, used for the response Content-Disposition.
        """
        connector = self.env["s3.connector"].sudo().browse(connector_id)
        bucket = self._bucket_for(connector)
        params = {
            "Bucket": bucket,
            "Key": object_key,
            "ResponseContentType": mimetype,
        }
        if filename:
            safe = filename.replace('"', '')
            params["ResponseContentDisposition"] = f'{disposition}; filename="{safe}"'
        else:
            params["ResponseContentDisposition"] = disposition
        try:
            return self._client_for(connector).generate_presigned_url(
                "get_object", Params=params, ExpiresIn=expires_in,
            )
        except (ClientError, BotoCoreError) as e:
            raise self._translate(e, "presigned_url") from e

    @api.model
    def verify_object_sha256(self, connector_id, object_key):
        """Re-read an S3 object and recompute its SHA-256."""
        connector = self.env["s3.connector"].sudo().browse(connector_id)
        bucket = self._bucket_for(connector)
        try:
            obj = self._client_for(connector).get_object(Bucket=bucket, Key=object_key)
        except (ClientError, BotoCoreError) as e:
            raise self._translate(e, "verify_sha256") from e
        sha = hashlib.sha256()
        body = obj["Body"]
        # ``iter_chunks`` is generator-style in newer botocore; falls back to
        # ``read()`` on older releases.
        try:
            for chunk in body.iter_chunks(1024 * 1024):
                sha.update(chunk)
        except AttributeError:
            data = body.read()
            sha.update(data)
        return sha.hexdigest()

    @api.model
    def upload_with_integrity(self, connector_id, object_key, file_path, *,
                              mimetype="video/mp4", acl="public-read"):
        """Upload a local file using ``TRANSFER_CONFIG``.

        Returns ``{"etag": str, "size": int}``. SHA-256 is computed by the
        caller during the streaming download from OpenRouter — we don't
        recompute here (that would require a full local re-read).
        """
        connector = self.env["s3.connector"].sudo().browse(connector_id)
        client = self._client_for(connector)
        bucket = self._bucket_for(connector)
        extra_args = {
            "ContentType": mimetype,
            "ContentDisposition": "inline",
            "CacheControl": "private, max-age=3600",
        }
        if acl:
            extra_args["ACL"] = acl
        try:
            with open(file_path, "rb") as f:
                client.upload_fileobj(
                    Fileobj=f, Bucket=bucket, Key=object_key,
                    ExtraArgs=extra_args, Config=TRANSFER_CONFIG,
                )
        except (ClientError, BotoCoreError) as e:
            raise self._translate(e, "upload") from e
        try:
            head = client.head_object(Bucket=bucket, Key=object_key)
        except (ClientError, BotoCoreError) as e:
            raise self._translate(e, "head_object") from e
        return {
            "etag": head["ETag"].strip('"'),
            "size": head["ContentLength"],
        }

    # ------------------------------------------------------------------
    # Error translation
    # ------------------------------------------------------------------
    @api.model
    def copy_object(self, connector_id, src_key, dst_key):
        """Server-side copy within the same bucket. Returns the destination ETag."""
        connector = self.env["s3.connector"].sudo().browse(connector_id)
        bucket = self._bucket_for(connector)
        try:
            resp = self._client_for(connector).copy_object(
                Bucket=bucket,
                Key=dst_key,
                CopySource={"Bucket": bucket, "Key": src_key},
                MetadataDirective="COPY",
            )
        except (ClientError, BotoCoreError) as e:
            raise self._translate(e, "copy_object") from e
        return (resp.get("CopyObjectResult") or {}).get("ETag") or ""

    @api.model
    def delete_object(self, connector_id, key):
        """Hard-delete a single object."""
        connector = self.env["s3.connector"].sudo().browse(connector_id)
        bucket = self._bucket_for(connector)
        try:
            self._client_for(connector).delete_object(Bucket=bucket, Key=key)
        except (ClientError, BotoCoreError) as e:
            raise self._translate(e, "delete_object") from e
        return True

    @api.model
    def _translate(self, exc, action):
        if isinstance(exc, NoCredentialsError):
            msg = "S3 credentials are not available"
        elif isinstance(exc, EndpointConnectionError):
            msg = f"S3 endpoint unreachable: {exc}"
        elif isinstance(exc, ClientError):
            code = (exc.response or {}).get('Error', {}).get('Code', '?')
            detail = (exc.response or {}).get('Error', {}).get('Message', str(exc))
            msg = f"S3 {action} failed [{code}]: {detail}"
        else:
            msg = f"S3 {action} failed: {exc}"
        _logger.warning("Crowley S3: %s", msg)
        return S3StorageError(msg)
