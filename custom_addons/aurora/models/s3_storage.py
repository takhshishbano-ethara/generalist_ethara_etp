import logging
import os
import re
import time
from typing import Optional

_logger = logging.getLogger(__name__)

_RUN_PREFIX_RE = re.compile(r"^run_(\d+)/$")

# ---------------------------------------------------------------------------
# S3 upload resilience constants
# ---------------------------------------------------------------------------
_S3_MAX_UPLOAD_ATTEMPTS = 3          # Application-level retries around upload_file
_S3_RETRY_BACKOFF_BASE = 4           # Seconds: 4, 8, 16 …
_S3_MULTIPART_THRESHOLD = 50 * 1024 * 1024   # 50 MB — files below this use single PUT
_S3_MULTIPART_CHUNKSIZE = 25 * 1024 * 1024   # 25 MB parts for files above threshold
_S3_MAX_CONCURRENCY = 4              # Parallel part uploads


def _get_client(s3_config: dict):
    import boto3
    from botocore.config import Config

    region = s3_config.get("region", "ap-south-1")
    return boto3.client(
        "s3",
        aws_access_key_id=s3_config["access_key"],
        aws_secret_access_key=s3_config["secret_key"],
        region_name=region,
        # Force regional endpoint — avoids DNS redirects on cross-continent uploads
        endpoint_url=f"https://s3.{region}.amazonaws.com",
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


def validate_credentials(s3_config: dict) -> None:
    _logger.info(
        "S3 validate: bucket=%r, region=%r",
        s3_config.get("bucket"),
        s3_config.get("region"),
    )
    client = _get_client(s3_config)
    client.head_bucket(Bucket=s3_config["bucket"])


def _build_base_prefix(org: str, repo: str, folder: str = "", phase: str = "aurora_phase1") -> str:
    """Return the S3 base prefix for a given phase.

    Layout: {folder}/{phase}/{org}__{repo}/ — or {phase}/{org}__{repo}/ when folder is empty.
    phase defaults to 'aurora_phase1' for backward compatibility with existing Phase-1 callers.
    """
    folder = folder.strip("/") if folder else ""
    phase = phase.strip("/") if phase else "aurora_phase1"
    if folder:
        return f"{folder}/{phase}/{org}__{repo}/"
    return f"{phase}/{org}__{repo}/"


def get_next_run_number(s3_config: dict, org: str, repo: str, folder: str = "", phase: str = "aurora_phase1") -> int:
    """Scan S3 for existing run_N folders under {folder}/{phase}/{org}__{repo}/ and return the next number.

    Phase-2 callers pass phase='aurora_phase2' to keep their run counter independent.
    """
    client = _get_client(s3_config)
    prefix = _build_base_prefix(org, repo, folder, phase)
    max_run = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=s3_config["bucket"], Prefix=prefix, Delimiter="/"
    ):
        for cp in page.get("CommonPrefixes", []):
            folder_name = cp["Prefix"][len(prefix):]
            m = _RUN_PREFIX_RE.match(folder_name)
            if m:
                max_run = max(max_run, int(m.group(1)))
    return max_run + 1


def upload_file(
    s3_config: dict, local_path: str, s3_key: str
) -> str:
    """Upload a local file to S3 with retry, detailed logging, and multipart tuning.

    Returns the public S3 URL on success.
    Raises on exhausted retries with a clear infrastructure-vs-code message.
    """
    bucket = s3_config["bucket"]
    region = s3_config.get("region", "ap-south-1")
    filename = os.path.basename(local_path)
    file_size = os.path.getsize(local_path)
    size_mb = file_size / (1024 * 1024)
    upload_mode = "multipart" if file_size > _S3_MULTIPART_THRESHOLD else "single PUT"

    _logger.info(
        "S3 upload: %s (%.1f MB, %s) -> s3://%s/%s",
        filename, size_mb, upload_mode, bucket, s3_key,
    )

    client = _get_client(s3_config)
    transfer_config = _get_transfer_config()
    last_error = None

    for attempt in range(1, _S3_MAX_UPLOAD_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            client.upload_file(
                local_path, bucket, s3_key, Config=transfer_config,
            )
            elapsed = time.monotonic() - t0
            speed_mbps = (size_mb / elapsed) if elapsed > 0 else 0
            _logger.info(
                "S3 upload: %s completed in %.1fs (%.1f MB/s)",
                filename, elapsed, speed_mbps,
            )
            return f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"
        except Exception as exc:
            elapsed = time.monotonic() - t0
            last_error = exc
            if attempt < _S3_MAX_UPLOAD_ATTEMPTS:
                backoff = _S3_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                _logger.warning(
                    "S3 upload: %s attempt %d/%d failed after %.1fs — %s: %s. "
                    "Retrying in %ds.",
                    filename, attempt, _S3_MAX_UPLOAD_ATTEMPTS,
                    elapsed, type(exc).__name__, exc, backoff,
                )
                time.sleep(backoff)
            else:
                _logger.error(
                    "S3 upload FAILED after %d attempts: %s (%.1f MB). "
                    "This is a network/infrastructure issue, not a code error. "
                    "Check connectivity to S3 region %s. "
                    "Last error: %s: %s",
                    _S3_MAX_UPLOAD_ATTEMPTS, filename, size_mb,
                    region, type(exc).__name__, exc,
                )

    raise last_error


def build_s3_key(org: str, repo: str, run_number: int, filename: str, folder: str = "", phase: str = "aurora_phase1") -> str:
    """Build the full S3 key for a file in a given phase's run.

    Result: {folder}/{phase}/{org}__{repo}/run_{N}/{filename}.
    Phase-2 callers pass phase='aurora_phase2' and may nest a pr-{N}/ segment inside filename.
    """
    base = _build_base_prefix(org, repo, folder, phase)
    return f"{base}run_{run_number}/{filename}"


def is_configured(s3_config: dict) -> bool:
    return bool(
        s3_config.get("bucket")
        and s3_config.get("access_key")
        and s3_config.get("secret_key")
    )


def generate_presigned_url(
    s3_config: dict, s3_key: str, expires_in: int = 3600
) -> str:
    client = _get_client(s3_config)
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": s3_config["bucket"], "Key": s3_key},
        ExpiresIn=expires_in,
    )


def download_file(s3_config: dict, s3_key: str, local_path: str) -> str:
    """Download an S3 object to a local file. Returns the local path.

    Uses the same transfer config as upload_file (multipart, multi-threaded).
    """
    client = _get_client(s3_config)
    transfer_config = _get_transfer_config()
    client.download_file(
        s3_config["bucket"], s3_key, local_path, Config=transfer_config,
    )
    return local_path


def upload_bytes(s3_config: dict, payload: bytes, s3_key: str) -> str:
    """Upload an in-memory bytes payload to S3. Convenience wrapper around upload_file."""
    import tempfile

    bucket = s3_config["bucket"]
    region = s3_config.get("region", "ap-south-1")
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(payload)
        tmp_path = tmp.name
    try:
        upload_file(s3_config, tmp_path, s3_key)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"


def parse_s3_url(url: str) -> Optional[tuple]:
    """Return (bucket, key) for an S3 HTTPS URL, or None if not parseable.

    Accepts:
      https://{bucket}.s3.{region}.amazonaws.com/{key}
      https://{bucket}.s3.amazonaws.com/{key}
      s3://{bucket}/{key}
    """
    if not url:
        return None
    if url.startswith("s3://"):
        rest = url[5:]
        if "/" not in rest:
            return None
        bucket, key = rest.split("/", 1)
        return (bucket, key)
    m = re.match(r"^https://([^./]+)\.s3(?:\.[^.]+)?\.amazonaws\.com/(.+)$", url)
    if m:
        return (m.group(1), m.group(2))
    return None
