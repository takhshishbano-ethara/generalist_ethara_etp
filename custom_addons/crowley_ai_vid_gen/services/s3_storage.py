# -*- coding: utf-8 -*-
import hashlib
import logging
import threading

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)
from boto3.s3.transfer import TransferConfig

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class CrowleyS3Storage(models.AbstractModel):
    _name = "crowley.ai.vid.gen.s3.storage"
    _description = "S3 storage for Crowley AI generated videos"

    # Module-level cache + lock — survives across AbstractModel calls within a worker.
    _client_cache = {}
    _client_lock = threading.Lock()

    TRANSFER_CONFIG = TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        multipart_chunksize=16 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )

    @api.model
    def _bucket(self):
        bucket = self.env['ir.config_parameter'].sudo().get_param('crowley_ai_vid_gen.s3_bucket')
        if not bucket:
            raise UserError(_("S3 bucket is not configured. Set it in Settings → Crowley AI Vid Gen."))
        return bucket

    @api.model
    def _client(self):
        """Return a boto3 S3 client.

        Cached per (db, region, access_key_fingerprint, secret_key_fingerprint,
        endpoint) — so rotating EITHER the access key or the secret key
        invalidates the cache automatically. We fingerprint the secret rather
        than store it in the cache key, both for hygiene (avoid keeping the
        raw secret in long-lived Python memory beyond what's already in the
        boto3 client) and to make cache-key debugging safe.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        db = self.env.cr.dbname
        region = ICP.get_param('crowley_ai_vid_gen.s3_region') or 'us-east-1'
        access_key = ICP.get_param('crowley_ai_vid_gen.s3_access_key') or ''
        secret_key = ICP.get_param('crowley_ai_vid_gen.s3_secret_key') or ''
        endpoint = ICP.get_param('crowley_ai_vid_gen.s3_endpoint_url') or ''

        secret_fp = (
            hashlib.sha256(secret_key.encode()).hexdigest()[:16]
            if secret_key else ""
        )
        cache_key = (db, region, access_key, secret_fp, endpoint)
        client = self._client_cache.get(cache_key)
        if client is not None:
            return client

        with self._client_lock:
            client = self._client_cache.get(cache_key)
            if client is not None:
                return client

            cfg = Config(
                retries={"max_attempts": 5, "mode": "adaptive"},
                connect_timeout=10,
                read_timeout=120,
                max_pool_connections=20,
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
            )
            kwargs = {
                "service_name": "s3",
                "region_name": region,
                "config": cfg,
            }
            if access_key and secret_key:
                kwargs["aws_access_key_id"] = access_key
                kwargs["aws_secret_access_key"] = secret_key
            if endpoint:
                kwargs["endpoint_url"] = endpoint
            elif region and region != "us-east-1":
                # Force the regional endpoint into the URL. Without this,
                # boto3 emits s3.amazonaws.com (the legacy global endpoint),
                # AWS returns 307 redirects to the regional host, and HTML5
                # <video> elements refuse to follow the redirect. Baking in
                # s3.<region>.amazonaws.com from the start makes presigned
                # URLs work in browsers across regions.
                kwargs["endpoint_url"] = f"https://s3.{region}.amazonaws.com"
            try:
                client = boto3.client(**kwargs)
            except NoCredentialsError as e:
                raise UserError(_(
                    "S3 credentials are not available. "
                    "Configure access_key/secret_key or attach an IAM role."
                )) from e
            _logger.info(
                "Crowley S3: client created for db=%s region=%s "
                "access_key=%s endpoint=%s",
                db, region,
                (access_key[:6] + "..." + access_key[-4:]) if access_key else "<aws-default>",
                endpoint or "<aws-default>",
            )
            self._client_cache[cache_key] = client
            _logger.info(
                "Crowley S3: client created for db=%s region=%s endpoint=%s",
                db, region, endpoint or "<aws-default>",
            )
            return client

    @api.model
    def clear_cache(self):
        with self._client_lock:
            self._client_cache.clear()

    @api.model
    def upload_fileobj(self, fileobj, *, bucket, key, mimetype="video/mp4"):
        """Upload a file-like object to S3 with multipart-aware streaming.

        :param fileobj: any object with .read(n) — including the OpenRouter HTTP response stream.
        :returns: the S3 ETag string (quotes stripped).
        """
        extra_args = {
            "ContentType": mimetype,
            "ContentDisposition": "inline",
            "CacheControl": "private, max-age=3600",
        }
        client = self._client()
        try:
            client.upload_fileobj(
                Fileobj=fileobj, Bucket=bucket, Key=key,
                ExtraArgs=extra_args, Config=self.TRANSFER_CONFIG,
            )
        except (ClientError, BotoCoreError) as e:
            raise self._translate_error(e, action="upload") from e
        try:
            head = client.head_object(Bucket=bucket, Key=key)
        except (ClientError, BotoCoreError) as e:
            raise self._translate_error(e, action="head_object") from e
        return head["ETag"].strip('"')

    @api.model
    def presigned_get_url(self, key, *, expires_in=300, mimetype="video/mp4",
                          disposition="inline", filename=None):
        """Generate a time-limited presigned GET URL.

        :param disposition: 'inline' (play in browser) or 'attachment' (download).
        :param filename: optional download filename (used for the response Content-Disposition).
        """
        params = {
            "Bucket": self._bucket(),
            "Key": key,
            "ResponseContentType": mimetype,
        }
        if filename:
            # Quote the filename to be safe — boto3 will URL-encode further if needed.
            safe_name = filename.replace('"', '')
            params["ResponseContentDisposition"] = f'{disposition}; filename="{safe_name}"'
        else:
            params["ResponseContentDisposition"] = disposition
        try:
            return self._client().generate_presigned_url(
                "get_object", Params=params, ExpiresIn=expires_in,
            )
        except (ClientError, BotoCoreError) as e:
            raise self._translate_error(e, action="presign") from e

    @api.model
    def verify_object_sha256(self, *, bucket, key):
        """Re-read an S3 object and recompute its SHA-256.

        Used by the optional ``verify_after_upload`` setting to prove
        byte-identity between the bytes that Seedance returned and the
        bytes we stored — at the cost of doubling S3 egress.
        """
        try:
            obj = self._client().get_object(Bucket=bucket, Key=key)
        except (ClientError, BotoCoreError) as e:
            raise self._translate_error(e, action="verify") from e
        sha = hashlib.sha256()
        body = obj["Body"]
        for chunk in body.iter_chunks(1024 * 1024):
            sha.update(chunk)
        return sha.hexdigest()

    @api.model
    def _translate_error(self, e, *, action):
        if isinstance(e, NoCredentialsError):
            return UserError(_("S3 credentials are not available."))
        if isinstance(e, EndpointConnectionError):
            return UserError(_("Cannot reach S3 endpoint: %s") % e)
        if isinstance(e, ClientError):
            code = (e.response or {}).get('Error', {}).get('Code', '?')
            msg = (e.response or {}).get('Error', {}).get('Message', str(e))
            if code == 'NoSuchBucket':
                return UserError(_("S3 bucket does not exist (action: %s).") % action)
            if code in ('AccessDenied', 'SignatureDoesNotMatch', 'InvalidAccessKeyId'):
                return UserError(_(
                    "S3 access denied (action: %s). Check credentials/IAM policy. Detail: %s"
                ) % (action, msg))
            return UserError(_("S3 error during %s: %s (%s)") % (action, msg, code))
        return UserError(_("S3 protocol error during %s: %s") % (action, e))
