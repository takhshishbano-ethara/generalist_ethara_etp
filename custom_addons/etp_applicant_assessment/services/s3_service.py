import logging
import time
import uuid

_logger = logging.getLogger(__name__)

_PARAM_PREFIX = "etp_applicant_assessment"
_PLACEHOLDER = "PLACEHOLDER"

_RETRYABLE_CODES = {
    "RequestTimeout", "RequestTimeTooSkewed", "Throttling",
    "ThrottlingException", "SlowDown", "InternalError",
    "ServiceUnavailable", "503", "500",
}


def _param(env, key, default=""):
    value = env["ir.config_parameter"].sudo().get_param(
        f"{_PARAM_PREFIX}.{key}", default,
    )
    if value == _PLACEHOLDER:
        return ""
    return value


def is_configured(env):
    return bool(
        _param(env, "s3_bucket")
        and _param(env, "s3_access_key_id")
        and _param(env, "s3_secret_key")
    )


def _client(env):
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        region_name=_param(env, "s3_region", "us-east-1"),
        aws_access_key_id=_param(env, "s3_access_key_id"),
        aws_secret_access_key=_param(env, "s3_secret_key"),
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _is_retryable(exc):
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return False
    if isinstance(exc, BotoCoreError) or isinstance(exc, ConnectionError):
        return True
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
        code = str(err.get("Code", ""))
        meta = exc.response.get("ResponseMetadata", {}) if hasattr(exc, "response") else {}
        status = str(meta.get("HTTPStatusCode", ""))
        return code in _RETRYABLE_CODES or status in _RETRYABLE_CODES
    return False


def _build_url(env, bucket, region, key):
    cdn = _param(env, "s3_cdn_url")
    if cdn:
        # Candidate + reviewer pages are always served over HTTPS, so
        # an http:// cdn value would fire mixed-content blocking in
        # the browser and leave the clip unplayable without any
        # console error surfaced to Odoo. Force HTTPS.
        if cdn.startswith("http://"):
            cdn = "https://" + cdn[len("http://"):]
        elif not cdn.startswith("https://"):
            cdn = "https://" + cdn.lstrip("/")
        return f"{cdn.rstrip('/')}/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def upload_bytes(env, data, key_hint="clip", content_type="video/webm",
                 extension=None, client=None):
    if not data:
        return "", ""
    bucket = _param(env, "s3_bucket")
    region = _param(env, "s3_region", "us-east-1")
    folder = _param(env, "s3_folder", "etp_applicant_assessment")
    try:
        max_retries = int(_param(env, "s3_max_retries", "3"))
    except (TypeError, ValueError):
        max_retries = 3

    ext = extension or "bin"
    key = f"{folder.rstrip('/')}/{key_hint}-{uuid.uuid4().hex}.{ext}"

    if client is None:
        client = _client(env)

    delay = 1
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
            return _build_url(env, bucket, region, key), key
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt >= max_retries:
                raise
            _logger.warning(
                "S3 upload retry %s/%s for key=%s (%s)",
                attempt, max_retries, key, exc,
            )
            time.sleep(delay)
            delay *= 2
    if last_exc:
        raise last_exc
    return "", ""


def delete_key(env, key, client=None):
    if not key:
        return
    bucket = _param(env, "s3_bucket")
    try:
        if client is None:
            client = _client(env)
        client.delete_object(Bucket=bucket, Key=key)
    except Exception:
        _logger.exception("S3 delete failed for key=%s", key)


def head_object(env, key, client=None):
    if not key:
        return False
    bucket = _param(env, "s3_bucket")
    if not bucket:
        return False
    try:
        if client is None:
            client = _client(env)
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def presigned_get_url(env, key, expires=3600, client=None):
    """Short-lived HTTPS GET the reviewer's browser can play (private bucket)."""
    if not key:
        return ""
    bucket = _param(env, "s3_bucket")
    if not bucket:
        return ""
    try:
        if client is None:
            client = _client(env)
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=int(expires),
        )
    except Exception:
        _logger.exception("presigned_get_url failed for key=%s", key)
        return ""


def generate_presigned_post(env, key_prefix, max_bytes=10 * 1024 * 1024,
                            content_type="video/webm", extension="webm",
                            expires=120, client=None):
    """Hand the browser a short-lived presigned POST to upload one clip
    DIRECT to S3.  Server generates the object key so the client cannot
    choose it; conditions pin Content-Type and cap object size."""
    import secrets
    bucket = _param(env, "s3_bucket")
    if not bucket:
        return None
    folder = _param(env, "s3_folder", "etp_applicant_assessment").rstrip("/")
    key = (
        f"{folder}/{key_prefix.strip('/')}/"
        f"{secrets.token_hex(16)}.{extension}"
    )
    if client is None:
        client = _client(env)
    try:
        presigned = client.generate_presigned_post(
            Bucket=bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, int(max_bytes)],
            ],
            ExpiresIn=int(expires),
        )
    except Exception:
        _logger.exception("generate_presigned_post failed for key=%s", key)
        return None
    return {
        "url": presigned["url"],
        "fields": presigned["fields"],
        "key": key,
    }
