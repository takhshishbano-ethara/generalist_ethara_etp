import base64
import logging
import time
import uuid

_logger = logging.getLogger(__name__)

_MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024

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
        _param(env, "etp_assessment_pro.s3_bucket")
        and _param(env, "etp_assessment_pro.s3_access_key_id")
        and _param(env, "etp_assessment_pro.s3_secret_key")
    )


def _max_retries(env):
    try:
        return max(1, int(_param(env, "etp_assessment_pro.s3_max_retries", "3")))
    except (TypeError, ValueError):
        return 3


_S3_CLIENT_CACHE = {}


def _client(env):
    import boto3
    from botocore.config import Config
    region = _param(env, "etp_assessment_pro.s3_region", "us-east-1")
    ak = _param(env, "etp_assessment_pro.s3_access_key_id")
    sk = _param(env, "etp_assessment_pro.s3_secret_key")
    cache_key = (ak, sk, region)
    cli = _S3_CLIENT_CACHE.get(cache_key)
    if cli is None:
        cli = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )
        _S3_CLIENT_CACHE[cache_key] = cli
    return cli


def _is_retryable(exc):
    from botocore.exceptions import ClientError, BotoCoreError, ConnectionError
    if isinstance(exc, (BotoCoreError, ConnectionError)):
        return True
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = str(exc.response.get("ResponseMetadata", {})
                     .get("HTTPStatusCode", ""))
        return code in _RETRYABLE_CODES or status in _RETRYABLE_CODES
    return False


def _build_url(env, bucket, region, key):
    cdn = _param(env, "etp_assessment_pro.s3_cdn_url")
    if cdn:
        return f"{cdn.rstrip('/')}/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def upload_b64(env, b64_data, key_hint="file", content_type="application/octet-stream",
               client=None):
    if not is_configured(env):
        raise ValueError(
            "S3 not configured. Set etp_assessment_pro.s3_bucket / "
            "s3_access_key_id / s3_secret_key in System Parameters."
        )
    if not b64_data:
        raise ValueError("Empty payload - nothing to upload.")

    bucket = _param(env, "etp_assessment_pro.s3_bucket")
    region = _param(env, "etp_assessment_pro.s3_region", "us-east-1")
    folder = _param(env, "etp_assessment_pro.s3_folder", "etp_assessment")
    ext = "bin"
    if "png" in content_type:
        ext = "png"
    elif "jpeg" in content_type or "jpg" in content_type:
        ext = "jpg"
    elif "mp4" in content_type:
        ext = "mp4"
    elif "webm" in content_type:
        ext = "webm"
    elif "json" in content_type:
        ext = "json"
    elif "text" in content_type:
        ext = "txt"
    key = f"{folder}/{key_hint}-{uuid.uuid4().hex}.{ext}"

    try:
        raw = base64.b64decode(b64_data)
    except Exception as exc:
        raise ValueError(f"Payload is not valid base64: {exc}")
    if not raw:
        raise ValueError("Decoded payload is empty - nothing to upload.")

    cli = client or _client(env)
    retries = _max_retries(env)
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            cli.put_object(
                Bucket=bucket, Key=key, Body=raw, ContentType=content_type
            )
            _logger.info(
                "etp_assessment uploaded s3://%s/%s (attempt %s)",
                bucket, key, attempt,
            )
            return _build_url(env, bucket, region, key), key
        except Exception as exc:
            last_exc = exc
            if attempt < retries and _is_retryable(exc):
                wait = 2 ** (attempt - 1)
                _logger.warning(
                    "S3 upload attempt %s/%s failed (%s) - retrying in %ss",
                    attempt, retries, exc, wait,
                )
                time.sleep(wait)
                continue
            _logger.error("S3 upload failed permanently for %s: %s", key, exc)
            raise
    raise last_exc if last_exc else RuntimeError("S3 upload failed")


def delete_key(env, key, client=None):
    if not key or not is_configured(env):
        return
    try:
        cli = client or _client(env)
        bucket = _param(env, "etp_assessment_pro.s3_bucket")
        cli.delete_object(Bucket=bucket, Key=key)
        _logger.info("etp_assessment cleaned up s3://%s/%s", bucket, key)
    except Exception as exc:
        _logger.warning("Failed to clean up S3 key %s: %s", key, exc)


def object_key_from_url(env, url):
    """Return the S3 object key when ``url`` points at OUR configured bucket
    (virtual-hosted S3 URL or the configured CDN), else None. Used to decide
    whether a stored image_url can be proxied from S3 or is an external URL."""
    if not url:
        return None
    from urllib.parse import urlparse, unquote
    bucket = _param(env, "etp_assessment_pro.s3_bucket")
    cdn = (_param(env, "etp_assessment_pro.s3_cdn_url") or "").rstrip("/")
    parsed = urlparse(url)
    key = unquote(parsed.path.lstrip("/"))
    if not key:
        return None
    host = parsed.netloc
    if bucket and host.startswith(bucket + ".s3") and host.endswith("amazonaws.com"):
        return key
    if bucket and host == bucket + ".s3.amazonaws.com":
        return key
    if cdn and url.startswith(cdn + "/"):
        return key
    return None


def presigned_url(env, key, expires=900, client=None):
    """Return a short-lived presigned GET URL for an object in the configured
    bucket, or ``None`` on failure. Lets the portal 302-redirect the browser
    straight to S3 (like a video ``video_url``) so a worker is not occupied for
    the whole transfer, WITHOUT exposing a permanently-public object: the URL
    is signed and expires after ``expires`` seconds. ``generate_presigned_url``
    is a local signing operation (no S3 round-trip)."""
    if not key or not is_configured(env):
        return None
    try:
        cli = client or _client(env)
        bucket = _param(env, "etp_assessment_pro.s3_bucket")
        return cli.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=max(60, int(expires or 900)),
        )
    except Exception as exc:
        _logger.warning("S3 presign failed for %s: %s", key, exc)
        return None


def download(env, key, client=None):
    """Fetch an object's bytes from the configured bucket, server-side and
    authenticated (no presigned URL, so nothing can 'expire'). Returns
    ``(bytes, content_type)`` or ``(None, None)`` on any failure."""
    if not key or not is_configured(env):
        return None, None
    try:
        cli = client or _client(env)
        bucket = _param(env, "etp_assessment_pro.s3_bucket")
        resp = cli.get_object(Bucket=bucket, Key=key)
        length = resp.get("ContentLength") or 0
        if length and length > _MAX_DOWNLOAD_BYTES:
            _logger.warning(
                "S3 object %s is %s bytes (> %s cap); not proxied through the "
                "worker.", key, length, _MAX_DOWNLOAD_BYTES)
            return None, None
        # M-4: ContentLength can be absent; read with a hard +1 cap so a missing
        # length can't stream unbounded bytes into worker memory.
        body = resp["Body"].read(_MAX_DOWNLOAD_BYTES + 1)
        if len(body) > _MAX_DOWNLOAD_BYTES:
            _logger.warning(
                "S3 object %s exceeded %s bytes on read (ContentLength absent); "
                "aborted.", key, _MAX_DOWNLOAD_BYTES)
            return None, None
        return body, resp.get("ContentType")
    except Exception as exc:
        _logger.warning("S3 download failed for %s: %s", key, exc)
        return None, None
