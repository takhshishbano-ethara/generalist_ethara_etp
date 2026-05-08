"""HTTP client for the external Leviathan extraction microservice."""

import ipaddress
import json
import logging
import socket
from urllib.parse import urlparse

import httpx

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 900  # Lambda extraction can take up to 15 min

_ALLOWED_SCHEMES = {"http", "https"}

_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n) for n in [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    ]
]


def validate_url(url: str) -> tuple[bool, str]:
    """Validate a URL for safety before sending to extraction service.

    Checks:
    - Scheme is http or https
    - Hostname is present and resolvable
    - Resolved IP is not in private/reserved ranges (SSRF protection)

    Returns:
        (is_valid, error_message) tuple.
    """
    if not url or not url.strip():
        return (False, "URL is empty")

    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        return (False, f"Invalid URL scheme '{parsed.scheme}'. Only http/https allowed.")

    hostname = parsed.hostname
    if not hostname:
        return (False, "URL has no hostname")

    try:
        resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return (False, f"Cannot resolve hostname '{hostname}'")

    for _family, _type, _proto, _canonname, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0])
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                return (False, "URL resolves to blocked private/reserved IP range")

    return (True, "")


def _extract_region_from_lambda_url(url: str) -> str:
    """Extract AWS region from Lambda Function URL hostname.
    Format: <id>.lambda-url.<region>.on.aws
    """
    try:
        parts = urlparse(url).hostname.split(".")
        idx = parts.index("lambda-url")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return "ap-south-1"


def trigger_extraction(
    url: str,
    job_id: int,
    callback_url: str,
    service_url: str,
    access_key_id: str,
    secret_access_key: str,
) -> dict:
    """Trigger extraction on the external Lambda service.

    Uses AWS SigV4 signing for Lambda Function URL authentication.

    Args:
        url: Website URL to extract.
        job_id: Odoo job record ID (passed to callback).
        callback_url: Webhook URL for completion notification.
        service_url: Lambda Function URL base.
        access_key_id: AWS access key for signing.
        secret_access_key: AWS secret key for signing.
    Returns:
        dict with 'success' bool and optional 'error' or 'extraction_id'.
    """
    is_valid, error_msg = validate_url(url)
    if not is_valid:
        _logger.warning(
            "URL validation failed for job %d: %s (url=%s)", job_id, error_msg, url
        )
        return {"success": False, "error": f"URL validation failed: {error_msg}"}

    if not service_url:
        return {"success": False, "error": "Extraction service URL not configured"}

    # Local dev mode: skip SigV4 for localhost URLs
    is_local = any(h in service_url for h in ["localhost", "127.0.0.1", "0.0.0.0"])

    try:
        endpoint = f"{service_url.rstrip('/')}/api/v1/extract"
        payload = json.dumps({
            "url": url,
            "job_id": job_id,
            "callback_url": callback_url,
        })
        headers = {"Content-Type": "application/json"}

        if not is_local:
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
            from botocore.credentials import Credentials

            region = _extract_region_from_lambda_url(service_url)
            credentials = Credentials(access_key_id, secret_access_key)
            aws_request = AWSRequest(
                method="POST",
                url=endpoint,
                data=payload,
                headers=headers,
            )
            SigV4Auth(credentials, "lambda", region).add_auth(aws_request)
            headers = dict(aws_request.headers)

        _logger.info("Triggering extraction for URL=%s, job_id=%d", url, job_id)

        with httpx.Client() as client:
            resp = client.post(
                endpoint,
                content=payload,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )

        if resp.status_code < 300:
            _json = resp.json()
            _logger.info("Extraction triggered successfully: %s", _json)
            return {"success": True, "extraction_id": _json.get("extraction_id")}

        error_text = resp.text
        _logger.warning("Extraction service error %d: %s", resp.status_code, error_text)
        return {"success": False, "error": f"Service returned {resp.status_code}"}

    except httpx.TimeoutException:
        return {"success": False, "error": "Extraction service timeout"}
    except httpx.HTTPError as exc:
        _logger.error("Extraction service HTTP error: %s", exc)
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.error("Extraction service call failed: %s", exc)
        return {"success": False, "error": str(exc)}
