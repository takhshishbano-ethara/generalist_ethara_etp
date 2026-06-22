import base64
import logging
import time
import uuid

_logger = logging.getLogger(__name__)

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


def _client(env):
    import boto3
    from botocore.config import Config
    region = _param(env, "etp_assessment_pro.s3_region", "us-east-1")
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=_param(env, "etp_assessment_pro.s3_access_key_id"),
        aws_secret_access_key=_param(env, "etp_assessment_pro.s3_secret_key"),
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


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
