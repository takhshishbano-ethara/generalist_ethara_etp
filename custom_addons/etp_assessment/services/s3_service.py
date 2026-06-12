# -*- coding: utf-8 -*-
"""S3 upload for assessment question media (generated images).

Mirrors the house pattern (leviathan/services/s3_service.py). Config comes
from System Parameters (placeholder-safe) so the feature is OFF until creds
are pasted in Settings > Technical > System Parameters:

    etp_assessment.s3_bucket
    etp_assessment.s3_region          (default us-east-1)
    etp_assessment.s3_access_key_id
    etp_assessment.s3_secret_key
    etp_assessment.s3_folder          (default etp_assessment)
    etp_assessment.s3_cdn_url         (optional CDN base)
    etp_assessment.s3_max_retries     (default 3)

Reliability:
  - each put_object is retried with exponential backoff on transient errors
  - upload_image_pair() uploads A+B atomically: if B fails after A succeeded,
    A's object is deleted so no orphan is left in the bucket.
"""
import base64
import logging
import time
import uuid

_logger = logging.getLogger(__name__)

# transient S3/HTTP errors worth retrying (network blips, throttling, 5xx)
_RETRYABLE_CODES = {
    "RequestTimeout", "RequestTimeTooSkewed", "Throttling",
    "ThrottlingException", "SlowDown", "InternalError",
    "ServiceUnavailable", "503", "500",
}


def _param(env, key, default=""):
    val = env["ir.config_parameter"].sudo().get_param(key, default)
    if val and "PLACEHOLDER" in str(val):
        return ""
    return val or default


def is_configured(env):
    return bool(
        _param(env, "etp_assessment.s3_bucket")
        and _param(env, "etp_assessment.s3_access_key_id")
        and _param(env, "etp_assessment.s3_secret_key")
    )


def _max_retries(env):
    try:
        return max(1, int(_param(env, "etp_assessment.s3_max_retries", "3")))
    except (TypeError, ValueError):
        return 3


def _client(env):
    import boto3
    from botocore.config import Config
    region = _param(env, "etp_assessment.s3_region", "us-east-1")
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=_param(env, "etp_assessment.s3_access_key_id"),
        aws_secret_access_key=_param(env, "etp_assessment.s3_secret_key"),
        # boto3's own retry layer on top of ours (belt + suspenders)
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _is_retryable(exc):
    from botocore.exceptions import ClientError, BotoCoreError, ConnectionError
    if isinstance(exc, (BotoCoreError, ConnectionError)):
        return True  # network-level: timeouts, connection resets, DNS
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = str(exc.response.get("ResponseMetadata", {})
                     .get("HTTPStatusCode", ""))
        return code in _RETRYABLE_CODES or status in _RETRYABLE_CODES
    return False


def _build_url(env, bucket, region, key):
    cdn = _param(env, "etp_assessment.s3_cdn_url")
    if cdn:
        return f"{cdn.rstrip('/')}/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def upload_image_b64(env, b64_data, key_hint="img", content_type="image/png",
                     client=None):
    """Upload a base64 image to S3 (with retry), return (url, key).

    Retries transient errors with exponential backoff. Raises ValueError when
    S3 is not configured (caller falls back to keeping the binary on the
    record); raises the last exception when all retries are exhausted.

    Pass an existing `client` to reuse a connection across a pair upload.
    """
    if not is_configured(env):
        raise ValueError(
            "S3 not configured. Set etp_assessment.s3_bucket / "
            "s3_access_key_id / s3_secret_key in System Parameters.")
    if not b64_data:
        raise ValueError("Empty image payload — nothing to upload.")

    bucket = _param(env, "etp_assessment.s3_bucket")
    region = _param(env, "etp_assessment.s3_region", "us-east-1")
    folder = _param(env, "etp_assessment.s3_folder", "etp_assessment")
    ext = "png" if "png" in content_type else "jpg"
    key = f"{folder}/{key_hint}-{uuid.uuid4().hex}.{ext}"

    try:
        raw = base64.b64decode(b64_data)
    except Exception as exc:
        raise ValueError(f"Image payload is not valid base64: {exc}")
    if not raw:
        raise ValueError("Decoded image is empty — nothing to upload.")

    cli = client or _client(env)
    retries = _max_retries(env)
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            cli.put_object(
                Bucket=bucket, Key=key, Body=raw, ContentType=content_type)
            _logger.info(
                "etp_assessment uploaded image s3://%s/%s (attempt %s)",
                bucket, key, attempt)
            return _build_url(env, bucket, region, key), key
        except Exception as exc:
            last_exc = exc
            if attempt < retries and _is_retryable(exc):
                wait = 2 ** (attempt - 1)  # 1s, 2s, 4s...
                _logger.warning(
                    "S3 upload attempt %s/%s failed (%s) — retrying in %ss",
                    attempt, retries, exc, wait)
                time.sleep(wait)
                continue
            _logger.error("S3 upload failed permanently for %s: %s", key, exc)
            raise
    # unreachable (loop always returns or raises), but keep the type checker happy
    raise last_exc if last_exc else RuntimeError("S3 upload failed")


def delete_key(env, key, client=None):
    """Best-effort delete of an S3 object (orphan cleanup). Never raises."""
    if not key or not is_configured(env):
        return
    try:
        cli = client or _client(env)
        bucket = _param(env, "etp_assessment.s3_bucket")
        cli.delete_object(Bucket=bucket, Key=key)
        _logger.info("etp_assessment cleaned up orphan s3://%s/%s", bucket, key)
    except Exception as exc:
        _logger.warning("Failed to clean up orphan S3 key %s: %s", key, exc)


def upload_image_pair(env, b64_a, b64_b, key_hint="q"):
    """Upload A and B atomically — return (url_a, url_b).

    If B fails after A succeeded, A's object is deleted so the bucket is left
    clean (no orphan). Reuses one client for both. Raises on failure so the
    caller marks the draft failed and re-runs the whole pair.
    """
    cli = _client(env)
    url_a, key_a = upload_image_b64(env, b64_a, key_hint=f"{key_hint}-a",
                                    client=cli)
    try:
        url_b, _key_b = upload_image_b64(env, b64_b, key_hint=f"{key_hint}-b",
                                         client=cli)
    except Exception:
        # B failed for good — roll back A so we don't leak an orphan
        delete_key(env, key_a, client=cli)
        raise
    return url_a, url_b
