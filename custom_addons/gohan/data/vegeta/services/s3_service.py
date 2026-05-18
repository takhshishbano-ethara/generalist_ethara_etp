"""S3 service for uploading Vegeta artifacts."""

import json
import logging
import mimetypes
import re
from io import BytesIO

import boto3
from botocore.exceptions import ClientError

_logger = logging.getLogger(__name__)


def _get_s3_client(access_key_id: str, secret_key: str, region: str):
    """Create boto3 S3 client."""
    kwargs = {
        "service_name": "s3",
        "region_name": region,
    }
    if access_key_id and secret_key:
        kwargs["aws_access_key_id"] = access_key_id
        kwargs["aws_secret_access_key"] = secret_key
    return boto3.client(**kwargs)


def upload_prd_to_s3(
    prd_text: str,
    job_name: str,
    bucket: str,
    access_key_id: str,
    secret_key: str,
    region: str,
    folder: str = "vegeta",
    cdn_url: str = "",
) -> str:
    """Upload PRD markdown to S3 and return CDN URL.

    Args:
        prd_text: PRD markdown content.
        job_name: Job reference (e.g., LEV-00001).
        bucket: S3 bucket name.
        access_key_id: AWS access key.
        secret_key: AWS secret key.
        region: AWS region.
        folder: S3 key prefix folder.
        cdn_url: CDN base URL (e.g., https://cdn.example.com).
    Returns:
        Public CDN URL to the uploaded PRD.
    """
    client = _get_s3_client(access_key_id, secret_key, region)
    key = f"{folder}/{job_name}/final_prd.md"

    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=prd_text.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        _logger.info("Uploaded PRD to s3://%s/%s", bucket, key)

        if cdn_url:
            return f"{cdn_url.rstrip('/')}/{key}"
        return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"

    except ClientError as exc:
        _logger.error("S3 upload failed: %s", exc)
        raise RuntimeError(f"S3 upload failed: {exc}") from exc


def upload_artifacts_to_s3(
    artifacts: dict,
    job_name: str,
    bucket: str,
    access_key_id: str,
    secret_key: str,
    region: str,
    folder: str = "vegeta",
    cdn_url: str = "",
) -> dict:
    """Upload extraction artifacts to S3.

    Args:
        artifacts: Dict mapping filename to content bytes or base64 string.
        job_name: Job reference.
        bucket: S3 bucket.
        access_key_id: AWS access key.
        secret_key: AWS secret key.
        region: AWS region.
        folder: S3 key prefix folder.
        cdn_url: CDN base URL.
    Returns:
        Dict mapping filename to CDN URLs.
    """
    client = _get_s3_client(access_key_id, secret_key, region)
    urls = {}
    base_key = f"{folder}/{job_name}/artifacts"

    for filename, content in artifacts.items():
        try:
            # Sanitize filename to prevent path traversal (S-4)
            safe_filename = re.sub(r'[^a-zA-Z0-9._/-]', '_', filename)[:200]
            safe_filename = safe_filename.lstrip('/')
            content_type = mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
            if isinstance(content, str):
                body = content.encode("utf-8")
            elif isinstance(content, bytes):
                body = content
            else:
                body = json.dumps(content).encode("utf-8")
                content_type = "application/json"

            key = f"{base_key}/{safe_filename}"
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
            if cdn_url:
                urls[filename] = f"{cdn_url.rstrip('/')}/{key}"
            else:
                urls[filename] = f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
        except Exception as exc:
            _logger.warning("Failed to upload %s: %s", filename, exc)

    _logger.info("Uploaded %d/%d artifacts for %s", len(urls), len(artifacts), job_name)
    return urls


def get_artifacts_folder_url(
    job_name: str,
    bucket: str,
    folder: str = "vegeta",
    cdn_url: str = "",
    **kwargs,
) -> str:
    """Get the folder URL for a job's artifacts."""
    key = f"{folder}/{job_name}/artifacts/"
    if cdn_url:
        return f"{cdn_url.rstrip('/')}/{key}"
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def download_file_from_s3(
    key: str,
    bucket: str,
    access_key_id: str = None,
    secret_key: str = None,
    region: str = "us-east-1",
) -> bytes:
    """Download a file from S3 and return its content as bytes."""
    client = _get_s3_client(access_key_id, secret_key, region)
    response = client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()
