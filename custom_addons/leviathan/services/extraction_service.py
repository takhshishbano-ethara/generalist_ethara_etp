"""Async invocation of the Leviathan extraction Lambda.

Calls ``lambda:Invoke`` with ``InvocationType='Event'`` so the call returns in
~50-200 ms regardless of how long the Lambda actually runs (6-15 min).  Results
arrive only via the webhook callback to ``/api/v1/leviathan/webhook/extraction-complete``.

This module replaces the previous synchronous ``httpx.post`` flow (which held
an Odoo thread open for the full 15-min Lambda timeout) and the previous
RabbitMQ + ``consumer.py`` fan-out (which is no longer needed).
"""

import ipaddress
import json
import logging
import socket
import threading
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

_logger = logging.getLogger(__name__)

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

_CLIENT_LOCK = threading.Lock()
_CLIENT_CACHE: dict[tuple, Any] = {}


def validate_url(url: str) -> tuple[bool, str]:
    """SSRF guard. Returns (is_valid, error_message)."""
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


def _get_lambda_client(region: str, access_key_id: str = "", secret_access_key: str = ""):
    """Cached boto3 Lambda client with a connection pool big enough for 250
    concurrent fan-out from a single Odoo worker."""
    cache_key = (region, access_key_id, secret_access_key)
    with _CLIENT_LOCK:
        client = _CLIENT_CACHE.get(cache_key)
        if client is not None:
            return client

        kwargs = {
            "service_name": "lambda",
            "region_name": region,
            "config": BotoConfig(
                max_pool_connections=300,
                connect_timeout=10,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        }
        if access_key_id and secret_access_key:
            kwargs["aws_access_key_id"] = access_key_id
            kwargs["aws_secret_access_key"] = secret_access_key

        client = boto3.client(**kwargs)
        _CLIENT_CACHE[cache_key] = client
        return client


def trigger_extraction(
    url: str,
    job_id: int,
    callback_url: str,
    function_name: str,
    region: str,
    access_key_id: str = "",
    secret_access_key: str = "",
) -> dict:
    """Fire-and-forget async invoke. Returns in <1s.

    Args:
        url: Website URL to extract.
        job_id: Odoo job record ID (echoed back in the webhook).
        callback_url: Webhook URL the Lambda will POST results to.
        function_name: Lambda function name or full ARN.
        region: AWS region of the Lambda.
        access_key_id / secret_access_key: Optional; falls back to pod-role
            (IRSA on EKS) or instance profile when omitted.

    Returns:
        dict with 'success' bool and optional 'error' or 'request_id'.
        'success=True' here means the invoke was accepted by AWS, NOT that
        extraction succeeded — that arrives only via the webhook.
    """
    is_valid, error_msg = validate_url(url)
    if not is_valid:
        _logger.warning(
            "URL validation failed for job %d: %s (url=%s)", job_id, error_msg, url
        )
        return {"success": False, "error": f"URL validation failed: {error_msg}"}

    if not function_name:
        return {"success": False, "error": "Lambda function name not configured"}

    payload = {
        "url": url,
        "job_id": job_id,
        "callback_url": callback_url,
    }

    try:
        client = _get_lambda_client(region, access_key_id, secret_access_key)
        response = client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        status = response.get("StatusCode")
        if status != 202:
            _logger.warning(
                "Lambda async invoke unexpected status %s for job %d (function=%s)",
                status, job_id, function_name,
            )
            return {
                "success": False,
                "error": f"Lambda invoke returned status {status} (expected 202)",
            }

        request_id = response.get("ResponseMetadata", {}).get("RequestId", "")
        _logger.info(
            "Lambda async invoke OK: job_id=%d, RequestId=%s, url=%s",
            job_id, request_id, url,
        )
        return {"success": True, "request_id": request_id}

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        _logger.error(
            "Lambda invoke ClientError for job %d [%s]: %s", job_id, code, msg
        )
        if code == "TooManyRequestsException":
            return {
                "success": False,
                "error": (
                    "Lambda reserved concurrency exhausted. "
                    "Raise ReservedConcurrentExecutions or throttle batch size."
                ),
            }
        if code == "ResourceNotFoundException":
            return {
                "success": False,
                "error": f"Lambda function '{function_name}' not found in region '{region}'",
            }
        if code in ("AccessDeniedException", "UnauthorizedOperation"):
            return {
                "success": False,
                "error": (
                    f"IAM denied lambda:InvokeFunction on '{function_name}'. "
                    "Check IRSA role / explicit access key permissions."
                ),
            }
        return {"success": False, "error": f"{code}: {msg}"}

    except Exception as exc:
        _logger.exception("Lambda async invoke failed for job %d", job_id)
        return {"success": False, "error": str(exc)}
