"""Crowley S3 publisher.

Downloads an MP4 from an arbitrary URL and uploads it to S3 via the
``crowley.s3.storage`` AbstractModel (which talks to boto3 directly and gives
us multipart streaming + presigned URLs + SHA-256 verification).

The ``s3.connector`` record is still the source of credentials (bucket name,
access key, secret, region, CDN URL). This module touches Odoo (calls
``env["s3.connector"]`` and ``env["crowley.s3.storage"]``) so it is NOT pure,
but it is designed to be called with a freshly-opened cursor and env from a
worker thread.
"""

import hashlib
import logging
import os
import tempfile
import time

import requests
from werkzeug.utils import secure_filename

# Re-export the S3 storage errors so callers can catch a single namespace.
from ..models.crowley_s3_storage import S3StorageError, S3VerificationError

_logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 300        # 5 min for the MP4 GET
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB hard cap
MAX_RETRIES = 3
RETRY_BACKOFF = (1, 3, 7)


class S3PublishError(Exception):
    pass


class S3DownloadError(S3PublishError):
    pass


class S3UploadError(S3PublishError):
    pass


# Re-export S3VerificationError under the publisher namespace.
__all__ = [
    "persist_video_to_s3",
    "S3PublishError",
    "S3DownloadError",
    "S3UploadError",
    "S3VerificationError",
    "S3StorageError",
]


def persist_video_to_s3(
    env,
    *,
    connector_id: int,
    record_id: int,
    record_name: str,
    mp4_url: str,
    prefix: str = "crowley/",
    object_key: str | None = None,
    auth_bearer: str | None = None,
    verify: bool = False,
) -> dict:
    """Download MP4 from ``mp4_url`` and upload to S3.

    Returns a dict::

        {
            "s3_bucket": str,
            "s3_key":    str,
            "s3_url":    str,   # public CDN URL (backward compat)
            "etag":      str,
            "sha256":    str,
            "size":      int,
        }

    When ``verify=True``, the uploaded object is re-read from S3 and its
    SHA-256 is compared against the streaming hash; mismatches raise
    :class:`S3VerificationError`.

    Raises :class:`S3DownloadError`, :class:`S3UploadError`, or
    :class:`S3VerificationError` on terminal failure.

    :param object_key: If provided, overrides the key construction entirely.
        Used for dataset-style filenames (T2AV_<category>_<NNNNNN>.mp4). When
        set, ``record_name`` and ``prefix`` are ignored for key construction
        (but ``record_name`` may still be used in error messages).
    """
    # 1. Resolve s3.connector record (credentials only).
    connector = env["s3.connector"].sudo().browse(connector_id)
    if not connector.exists():
        raise S3PublishError(f"s3.connector id={connector_id} not found")
    bucket = connector.name
    if not bucket:
        raise S3PublishError(f"s3.connector id={connector_id} has no bucket name")

    # 2. Build target key.
    if object_key:
        key = object_key
    else:
        safe_name = secure_filename(record_name) or f"video_{record_id}"
        key = f"{prefix}{record_id}/{safe_name}.mp4".lstrip("/")

    # 3. Download to temp file (computing SHA-256 inline).
    tmp_path = None
    try:
        tmp_path, total_bytes, sha256_hex = _download_to_temp(
            mp4_url, auth_bearer=auth_bearer,
        )

        # 4. Upload to S3 via the new storage helper (multipart-aware).
        storage = env["crowley.s3.storage"]
        upload_info = _upload_with_retry(storage, connector_id, key, tmp_path)

        # 5. Optional verification — re-reads the object from S3 and
        # recomputes SHA-256. Doubles S3 egress but proves byte identity.
        if verify:
            remote_sha = storage.verify_object_sha256(connector_id, key)
            if remote_sha != sha256_hex:
                raise S3VerificationError(
                    f"SHA-256 mismatch for s3://{bucket}/{key}: "
                    f"streaming={sha256_hex} remote={remote_sha}"
                )

        # Backward-compatible public CDN URL.
        cdn = (connector.cdn_url or "").rstrip("/")
        s3_url = f"{cdn}/{key}" if cdn else f"https://{bucket}.s3.amazonaws.com/{key}"

        return {
            "s3_bucket": bucket,
            "s3_key": key,
            "s3_url": s3_url,
            "etag": upload_info["etag"],
            "sha256": sha256_hex,
            "size": upload_info.get("size") or total_bytes,
        }
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                _logger.warning("Crowley: failed to delete temp file %s", tmp_path)


def _download_to_temp(url: str, *, auth_bearer: str | None = None) -> tuple[str, int, str]:
    """Download URL to a NamedTemporaryFile.

    Returns ``(path, total_bytes, sha256_hex)``.
    """
    # OpenRouter's "unsigned_urls" are actually authenticated API endpoints
    # (/api/v1/videos/{id}/content?index=0) that require the same Bearer token.
    headers = {"Authorization": f"Bearer {auth_bearer}"} if auth_bearer else {}
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(
                url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers=headers,
            ) as resp:
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(
                    suffix=".mp4", delete=False,
                ) as tmp:
                    sha256 = hashlib.sha256()
                    total = 0
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            tmp.close()
                            os.unlink(tmp.name)
                            raise S3DownloadError(
                                f"Video exceeds max size ({MAX_DOWNLOAD_BYTES} bytes)"
                            )
                        sha256.update(chunk)
                        tmp.write(chunk)
                    return tmp.name, total, sha256.hexdigest()
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
            last_exc = e
            _logger.warning(
                "Crowley: download attempt %d/%d failed: %s",
                attempt + 1, MAX_RETRIES, e,
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF[attempt])
    raise S3DownloadError(f"Download failed after {MAX_RETRIES} attempts: {last_exc}")


def _upload_with_retry(storage, connector_id: int, key: str, tmp_path: str) -> dict:
    """Upload local file via ``crowley.s3.storage.upload_with_integrity``.

    Returns the dict ``{"etag": str, "size": int}`` from the helper.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            info = storage.upload_with_integrity(connector_id, key, tmp_path)
            if not info or not info.get("etag"):
                raise S3UploadError("upload_with_integrity returned empty result")
            return info
        except S3StorageError as e:
            # Storage errors are already translated; retry on transient ones.
            last_exc = e
            _logger.warning(
                "Crowley: S3 upload attempt %d/%d failed: %s",
                attempt + 1, MAX_RETRIES, e,
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF[attempt])
        except Exception as e:  # pragma: no cover — defensive catch
            last_exc = e
            _logger.warning(
                "Crowley: S3 upload attempt %d/%d failed: %s",
                attempt + 1, MAX_RETRIES, e,
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF[attempt])
    raise S3UploadError(f"S3 upload failed after {MAX_RETRIES} attempts: {last_exc}")
