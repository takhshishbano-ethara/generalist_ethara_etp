"""S3 helpers for Jaeger K8s worker pods.

Reads config from environment variables. Provides retry with exponential
backoff and multipart upload tuning for large files.
"""
import logging
import os
import time

_logger = logging.getLogger(__name__)

_S3_MAX_UPLOAD_ATTEMPTS = 3
_S3_RETRY_BACKOFF_BASE = 4  # seconds: 4, 8, 16
_S3_MULTIPART_THRESHOLD = 50 * 1024 * 1024  # 50 MB
_S3_MULTIPART_CHUNKSIZE = 25 * 1024 * 1024  # 25 MB
_S3_MAX_CONCURRENCY = 4


def _get_client():
    import boto3
    from botocore.config import Config

    region = os.environ.get("JAEGER_S3_REGION", "ap-south-1")
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=os.environ.get("JAEGER_S3_ENDPOINT", f"https://s3.{region}.amazonaws.com"),
        config=Config(
            retries={"mode": "standard", "max_attempts": 5},
            connect_timeout=30,
            read_timeout=60,
            max_pool_connections=10,
        ),
    )


def _get_transfer_config():
    from boto3.s3.transfer import TransferConfig

    return TransferConfig(
        multipart_threshold=_S3_MULTIPART_THRESHOLD,
        multipart_chunksize=_S3_MULTIPART_CHUNKSIZE,
        max_concurrency=_S3_MAX_CONCURRENCY,
        use_threads=True,
    )


def _bucket():
    bucket = os.environ.get("JAEGER_S3_BUCKET", "")
    if not bucket:
        raise RuntimeError("JAEGER_S3_BUCKET environment variable is not set")
    return bucket


def _prefix():
    return os.environ.get("JAEGER_S3_PREFIX", "jaeger/phase1")


def s3_key(repo_id, filename):
    return f"{_prefix()}/{repo_id}/{filename}"


def upload(local_path, repo_id, filename):
    """Upload with retry + multipart tuning. Returns S3 key."""
    key = s3_key(repo_id, filename)
    bucket = _bucket()
    local_path = str(local_path)
    file_size = os.path.getsize(local_path)
    size_mb = file_size / (1024 * 1024)

    client = _get_client()
    transfer_config = _get_transfer_config()
    last_error = None

    for attempt in range(1, _S3_MAX_UPLOAD_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            client.upload_file(local_path, bucket, key, Config=transfer_config)
            elapsed = time.monotonic() - t0
            speed = (size_mb / elapsed) if elapsed > 0 else 0
            _logger.info(
                "S3 upload: %s (%.1f MB) -> s3://%s/%s in %.1fs (%.1f MB/s)",
                filename, size_mb, bucket, key, elapsed, speed,
            )
            return key
        except Exception as exc:
            last_error = exc
            if attempt < _S3_MAX_UPLOAD_ATTEMPTS:
                backoff = _S3_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                _logger.warning(
                    "S3 upload attempt %d/%d failed for %s: %s. Retrying in %ds.",
                    attempt, _S3_MAX_UPLOAD_ATTEMPTS, filename, exc, backoff,
                )
                time.sleep(backoff)
            else:
                _logger.error(
                    "S3 upload FAILED after %d attempts: %s (%.1f MB). %s",
                    _S3_MAX_UPLOAD_ATTEMPTS, filename, size_mb, exc,
                )

    raise last_error


def download(repo_id, filename, local_path):
    key = s3_key(repo_id, filename)
    bucket = _bucket()
    _get_client().download_file(bucket, key, str(local_path))
    _logger.info("S3 download: s3://%s/%s -> %s", bucket, key, local_path)
    return str(local_path)


def delete(repo_id, filename):
    key = s3_key(repo_id, filename)
    bucket = _bucket()
    _get_client().delete_object(Bucket=bucket, Key=key)
    _logger.info("S3 delete: s3://%s/%s", bucket, key)


def exists(repo_id, filename):
    from botocore.exceptions import ClientError

    key = s3_key(repo_id, filename)
    try:
        _get_client().head_object(Bucket=_bucket(), Key=key)
        return True
    except ClientError:
        return False


def delete_prefix(repo_id):
    prefix = f"{_prefix()}/{repo_id}/"
    bucket = _bucket()
    client = _get_client()
    paginator = client.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = page.get("Contents", [])
        if not objects:
            continue
        delete_keys = [{"Key": obj["Key"]} for obj in objects]
        client.delete_objects(Bucket=bucket, Delete={"Objects": delete_keys})
        deleted += len(delete_keys)
    if deleted:
        _logger.info("S3 cleanup: deleted %d objects under %s", deleted, prefix)
