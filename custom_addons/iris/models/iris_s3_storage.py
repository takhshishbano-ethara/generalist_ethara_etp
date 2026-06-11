"""Iris S3 storage helper (private PUT + presigned GET).

A thin AbstractModel adapted from ``crowley/models/crowley_s3_storage.py``.
It reads the ``s3.connector`` record for credentials (bucket name + access
key + secret + region) but talks to boto3 directly: the stock
``s3.connector.upload_to_s3`` API hardcodes ``ACL='public-read'``, which is
wrong for PII resumes. Iris uploads are PRIVATE objects, read back only via
short-lived presigned GET URLs.

The bucket name lives in the ``name`` field on ``s3.connector`` (verified
against ``custom_addons/s3_connector/models/s3_connector.py``:
``name = fields.Char(string="Bucket Name", required=True)``).
"""

import hashlib
import logging
import threading

import boto3
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


class S3StorageError(Exception):
    """Generic Iris S3 storage error (raised back to the caller)."""


# Thread-safe client cache: key = (db, connector_id, secret_fingerprint, region).
# Module-level so it survives across AbstractModel invocations in a worker.
_CLIENT_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


class IrisS3Storage(models.AbstractModel):
    _name = "iris.s3.storage"
    _description = "Iris S3 storage helper (private PUT + presigned GET)"

    # ------------------------------------------------------------------
    # Connector + client
    # ------------------------------------------------------------------
    @api.model
    def _client_for(self, connector):
        """Return a (cached) boto3 S3 client for the given ``s3.connector``.

        The cache key includes a fingerprint of the secret key so that
        rotating credentials on the connector record automatically
        invalidates the cached client.
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
                read_timeout=60,
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
            # return 307 redirects that some HTTP clients refuse to follow.
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
                "Iris S3: client created for connector_id=%s region=%s",
                connector.id, region or "<aws-default>",
            )
            return client

    @api.model
    def _bucket_for(self, connector):
        """Return the bucket name for a connector (the ``name`` field)."""
        if not connector or not connector.exists():
            raise UserError(_("S3 connector not found."))
        if not connector.name:
            raise UserError(_("S3 connector has no bucket name configured."))
        return connector.name

    @api.model
    def clear_cache(self):
        """Drop all cached boto3 clients (e.g. after credential rotation)."""
        with _CACHE_LOCK:
            _CLIENT_CACHE.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @api.model
    def presigned_get_url(self, connector_id, object_key, *,
                          expires_in=300, mimetype="application/pdf",
                          disposition="inline", filename=None):
        """Generate a time-limited SigV4 presigned GET URL.

        :param connector_id: id of the ``s3.connector`` record.
        :param object_key: S3 object key to read.
        :param expires_in: URL lifetime in seconds (default 300).
        :param mimetype: forced ``ResponseContentType`` of the download.
        :param disposition: ``inline`` (open in browser) or ``attachment``.
        :param filename: optional filename for the Content-Disposition.
        :return: the presigned URL string.
        :raises S3StorageError: on any boto3/client failure.
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
    def put_object_bytes(self, connector_id, object_key, data, *,
                         mimetype="application/pdf"):
        """Upload raw bytes as a PRIVATE S3 object (no public ACL).

        :param connector_id: id of the ``s3.connector`` record.
        :param object_key: destination S3 object key.
        :param data: raw bytes to store (NOT base64).
        :param mimetype: stored ``ContentType``.
        :return: dict ``{"etag": str, "size": int}``.
        :raises S3StorageError: on any boto3/client failure.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise S3StorageError("put_object_bytes expects raw bytes")
        connector = self.env["s3.connector"].sudo().browse(connector_id)
        client = self._client_for(connector)
        bucket = self._bucket_for(connector)
        try:
            response = client.put_object(
                Bucket=bucket,
                Key=object_key,
                Body=bytes(data),
                ContentType=mimetype,
                ContentDisposition="inline",
                CacheControl="private, max-age=0",
                # Deliberately NO ACL: the object stays private and is only
                # readable through presigned GET URLs.
            )
        except (ClientError, BotoCoreError) as e:
            raise self._translate(e, "put_object") from e
        return {
            "etag": (response.get("ETag") or "").strip('"'),
            "size": len(data),
        }

    # ------------------------------------------------------------------
    # Error translation
    # ------------------------------------------------------------------
    @api.model
    def _translate(self, exc, action):
        """Map a boto3 exception to an :class:`S3StorageError` with context."""
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
        _logger.warning("Iris S3: %s", msg)
        return S3StorageError(msg)
