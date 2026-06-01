# -*- coding: utf-8 -*-
import logging
import os
import time
from urllib.parse import urlparse

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_S3_MAX_UPLOAD_ATTEMPTS = 3
_S3_RETRY_BACKOFF_BASE = 4
_S3_MULTIPART_THRESHOLD = 50 * 1024 * 1024
_S3_MULTIPART_CHUNKSIZE = 25 * 1024 * 1024
_S3_MAX_CONCURRENCY = 4
_S3_ENDPOINT_ENV = "VIDEO_EDITOR_S3_ENDPOINT"


def _endpoint_override():
    return os.environ.get(_S3_ENDPOINT_ENV, "").strip()


def _get_client(s3_config: dict):
    import boto3
    from botocore.config import Config

    region = s3_config.get("region", "ap-south-1")
    client_kwargs = {
        "region_name": region,
        "config": Config(
            retries={"mode": "standard", "max_attempts": 5},
            connect_timeout=30,
            read_timeout=60,
            max_pool_connections=10,
        ),
    }
    endpoint_override = _endpoint_override()
    if endpoint_override:
        client_kwargs["endpoint_url"] = endpoint_override
    access_key = s3_config.get("access_key")
    secret_key = s3_config.get("secret_key")
    if access_key and secret_key:
        client_kwargs["aws_access_key_id"] = access_key
        client_kwargs["aws_secret_access_key"] = secret_key
    return boto3.client("s3", **client_kwargs)


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


def build_url(bucket: str, region: str, s3_key: str) -> str:
    endpoint_override = _endpoint_override()
    if endpoint_override:
        return f"{endpoint_override.rstrip('/')}/{bucket}/{s3_key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"


def is_configured(s3_config: dict) -> bool:
    return bool(s3_config.get("bucket"))


def parse_s3_url(s3_url: str):
    if not s3_url:
        return None, None
    if s3_url.startswith("s3://"):
        rest = s3_url[len("s3://"):]
        if "/" not in rest:
            return None, None
        bucket, key = rest.split("/", 1)
        if not bucket or not key:
            return None, None
        return bucket, key
    parsed = urlparse(s3_url)
    if not parsed.netloc or not parsed.path:
        return None, None
    host = parsed.netloc
    path = parsed.path.lstrip("/")
    if host.endswith(".amazonaws.com"):
        if host.startswith("s3.") or host.startswith("s3-"):
            if "/" not in path:
                return None, None
            bucket, key = path.split("/", 1)
            if not bucket or not key:
                return None, None
            return bucket, key
        bucket = host.split(".")[0]
        if not bucket or not path:
            return None, None
        return bucket, path
    if "/" not in path:
        return None, None
    bucket, key = path.split("/", 1)
    if not bucket or not key:
        return None, None
    return bucket, key


def upload_file(s3_config: dict, local_path: str, s3_key: str) -> str:
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
            client.upload_file(local_path, bucket, s3_key, Config=transfer_config)
            elapsed = time.monotonic() - t0
            speed_mbps = (size_mb / elapsed) if elapsed > 0 else 0
            _logger.info(
                "S3 upload: %s completed in %.1fs (%.1f MB/s)",
                filename, elapsed, speed_mbps,
            )
            return build_url(bucket, region, s3_key)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            last_error = exc
            if attempt < _S3_MAX_UPLOAD_ATTEMPTS:
                backoff = _S3_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                _logger.warning(
                    "S3 upload: %s attempt %d/%d failed after %.1fs - %s: %s. Retrying in %ds.",
                    filename, attempt, _S3_MAX_UPLOAD_ATTEMPTS,
                    elapsed, type(exc).__name__, exc, backoff,
                )
                time.sleep(backoff)
            else:
                _logger.error(
                    "S3 upload FAILED after %d attempts: %s (%.1f MB). "
                    "Network/infrastructure issue, not a code error. "
                    "Region=%s. Last error: %s: %s",
                    _S3_MAX_UPLOAD_ATTEMPTS, filename, size_mb,
                    region, type(exc).__name__, exc,
                )

    raise last_error


def download_to_file(s3_config: dict, s3_url_or_key: str, local_path: str,
                     bucket_override: str | None = None,
                     max_size_bytes: int | None = None,
                     progress_cb=None) -> int:
    if s3_url_or_key.startswith(("s3://", "http://", "https://")):
        bucket, key = parse_s3_url(s3_url_or_key)
        if not bucket or not key:
            raise UserError(_(
                "Could not parse S3 URL: %s"
            ) % s3_url_or_key)
    else:
        if not bucket_override:
            raise ValueError("Raw S3 key supplied without bucket_override.")
        bucket, key = bucket_override, s3_url_or_key.lstrip("/")

    client = _get_client(s3_config)
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise UserError(_(
            "Could not access s3://%(bucket)s/%(key)s : %(err)s"
        ) % {"bucket": bucket, "key": key, "err": exc}) from exc

    total_bytes = int(head.get("ContentLength") or 0)
    if max_size_bytes and total_bytes and total_bytes > max_size_bytes:
        raise UserError(_(
            "Source object is %(size).1f MB which exceeds the configured "
            "max of %(cap).1f MB."
        ) % {
            "size": total_bytes / (1024 * 1024),
            "cap": max_size_bytes / (1024 * 1024),
        })

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    t0 = time.monotonic()
    written = 0

    def _on_progress(chunk_bytes):
        nonlocal written
        written += chunk_bytes
        if progress_cb:
            try:
                progress_cb(written, total_bytes)
            except Exception:
                _logger.debug("download progress callback raised", exc_info=True)

    transfer_config = _get_transfer_config()
    try:
        client.download_file(
            bucket, key, local_path,
            Config=transfer_config,
            Callback=_on_progress,
        )
    except Exception as exc:
        raise UserError(_(
            "S3 download failed for s3://%(bucket)s/%(key)s : %(err)s"
        ) % {"bucket": bucket, "key": key, "err": exc}) from exc

    final_size = os.path.getsize(local_path)
    elapsed = time.monotonic() - t0
    mb = final_size / (1024 * 1024)
    _logger.info(
        "S3 download: s3://%s/%s -> %s (%.1f MB in %.1fs, %.1f MB/s)",
        bucket, key, local_path, mb, elapsed, (mb / elapsed) if elapsed > 0 else 0,
    )
    return final_size


def generate_presigned_url(s3_config: dict, s3_key: str, expires_in: int = 3600) -> str:
    client = _get_client(s3_config)
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": s3_config["bucket"], "Key": s3_key},
        ExpiresIn=expires_in,
    )


def head_object_exists(s3_config: dict, s3_key: str) -> bool:
    if not is_configured(s3_config) or not s3_key:
        return False
    from botocore.exceptions import ClientError
    client = _get_client(s3_config)
    try:
        client.head_object(Bucket=s3_config["bucket"], Key=s3_key)
        return True
    except ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


class S3SettingsResolver(models.AbstractModel):
    _name = "video.editor.s3.settings"
    _description = "Crowly Sourcing Configuration Resolver"

    @api.model
    def get_s3_config(self) -> dict:
        ICP = self.env["ir.config_parameter"].sudo()
        return {
            "bucket": (ICP.get_param("video_editor_s3.aws_bucket") or "").strip(),
            "region": (ICP.get_param("video_editor_s3.aws_region") or "ap-south-1").strip(),
            "access_key": (ICP.get_param("video_editor_s3.aws_access_key") or "").strip(),
            "secret_key": (ICP.get_param("video_editor_s3.aws_secret_key") or "").strip(),
        }

    @api.model
    def get_max_source_size_bytes(self) -> int:
        ICP = self.env["ir.config_parameter"].sudo()
        raw = ICP.get_param("video_editor_s3.max_source_size_mb") or "5120"
        try:
            return int(raw) * 1024 * 1024
        except (TypeError, ValueError):
            return 5120 * 1024 * 1024

    @api.model
    def get_max_concurrent_jobs(self) -> int:
        ICP = self.env["ir.config_parameter"].sudo()
        raw = ICP.get_param("video_editor_s3.max_concurrent_jobs") or "2"
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 2

    @api.model
    def get_default_export_prefix(self) -> str:
        ICP = self.env["ir.config_parameter"].sudo()
        return (ICP.get_param("video_editor_s3.export_prefix") or "video_editor_s3/exports").strip("/")

    @api.model
    def get_youtube_prefix(self) -> str:
        ICP = self.env["ir.config_parameter"].sudo()
        return (ICP.get_param("video_editor_s3.youtube_prefix") or "video_editor_s3/youtube").strip("/")

    @api.model
    def get_bedrock_config(self) -> dict:
        ICP = self.env["ir.config_parameter"].sudo()
        return {
            "region": (ICP.get_param("video_editor_s3.bedrock_region") or "us-east-1").strip(),
            "model_id": (ICP.get_param("video_editor_s3.bedrock_model_id") or "").strip(),
            "api_key": (ICP.get_param("video_editor_s3.bedrock_api_key") or "").strip(),
        }

    @api.model
    def get_qc_seed_prompt(self) -> str:
        import base64
        from . import llm_qc
        ICP = self.env["ir.config_parameter"].sudo()

        b64 = (ICP.get_param("video_editor_s3.qc_seed_file") or "").strip()
        if b64:
            try:
                text = base64.b64decode(b64).decode("utf-8").strip()
                if text:
                    return text
            except (ValueError, UnicodeDecodeError) as exc:
                _logger.warning(
                    "Stored QC seed file is invalid (%s) — falling back to bundled default.",
                    exc,
                )

        return llm_qc.load_default_seed_prompt()

    @api.model
    def get_youtube_ingest_config(self) -> dict:
        ICP = self.env["ir.config_parameter"].sudo()
        return {
            "cookies_browser": (ICP.get_param("video_editor_s3.yt_cookies_browser") or "").strip(),
            "cookies_path": (ICP.get_param("video_editor_s3.yt_cookies_path") or "").strip(),
            "proxy_url": (ICP.get_param("video_editor_s3.yt_proxy_url") or "").strip(),
        }
