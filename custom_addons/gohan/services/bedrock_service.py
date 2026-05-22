"""AWS Bedrock Converse API client for PRD generation.

Supports two authentication modes:
1. Bearer token (ABSK format) — for application inference profiles
2. SigV4 (IAM credentials) — standard boto3 auth (instance profile/IRSA/explicit keys)
"""

import json
import logging
import time
from typing import Optional

import boto3
import httpx
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, ReadTimeoutError

_logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT = 300
DEFAULT_TEMPERATURE = 0.7


def _is_bearer_token(access_key_id: str) -> bool:
    """Check if the provided key is a Bedrock bearer token (ABSK format)."""
    return bool(access_key_id and access_key_id.startswith("ABSK"))


def get_credentials(env) -> tuple[Optional[str], Optional[str]]:
    """Resolve Bedrock credentials from Odoo configuration only.

    Single source of truth: ``ir.config_parameter`` (Settings → Gohan).

      * Access key / bearer token: ``gohan.bedrock_access_key_id``
      * Secret key (SigV4 / AKIA only): ``gohan.bedrock_secret_access_key``

    Returning ``(None, None)`` is the expected "no credentials configured"
    signal — ``_get_bedrock_client`` then falls back to the pod IAM role
    (IRSA on EKS, instance profile on EC2). This keeps the rotate-via-DB
    workflow first-class: an operator updates the Settings UI, the next
    Bedrock call picks up the new value without restarting Odoo.
    """
    ICP = env["ir.config_parameter"].sudo()

    access_key = ICP.get_param("gohan.bedrock_access_key_id") or None
    secret_key = ICP.get_param("gohan.bedrock_secret_access_key") or None

    # Redacted preview: enough to recognize the value, never enough to leak it.
    # Operators chasing 403s use this log to confirm Odoo really loaded the
    # key they pasted into Settings (vs. a stale cached copy).
    if access_key:
        preview = f"{access_key[:6]}...{access_key[-4:]} (len={len(access_key)})"
        source = "ir.config_parameter:gohan.bedrock_access_key_id"
    else:
        preview = "<empty>"
        source = "none (will fall through to IRSA pod role)"
    _logger.info("Bedrock credential source: %s -> %s", source, preview)

    return access_key, secret_key


def _call_bedrock_bearer(
    inference_arn: str,
    region: str,
    bearer_token: str,
    system_prompt: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
) -> str:
    """Call Bedrock Converse API using bearer token authentication."""
    import urllib.parse

    model_id = urllib.parse.quote(inference_arn, safe="")
    endpoint = f"https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"

    bedrock_messages = []
    for msg in messages:
        content = msg["content"]
        # Support mixed content: string → text block, list → pass through
        if isinstance(content, str):
            bedrock_messages.append({"role": msg["role"], "content": [{"text": content}]})
        elif isinstance(content, list):
            bedrock_messages.append({"role": msg["role"], "content": content})
        else:
            bedrock_messages.append({"role": msg["role"], "content": [{"text": str(content)}]})

    payload = {
        "system": [{"text": system_prompt}],
        "messages": bedrock_messages,
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bearer_token}",
    }

    _logger.info(
        "[gohan] Calling Bedrock Converse (bearer): model=%s region=%s "
        "messages=%d max_tokens=%d",
        inference_arn,
        region,
        len(messages),
        max_tokens,
    )

    _call_t0 = time.monotonic()
    last_exc = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=30, read=DEFAULT_TIMEOUT, write=30, pool=30)) as client:
                resp = client.post(endpoint, json=payload, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                _logger.info(
                    "[gohan] Bedrock bearer OK in %.1fs — input_tokens=%d "
                    "output_tokens=%d stop_reason=%s attempt=%d/3",
                    time.monotonic() - _call_t0,
                    data.get("usage", {}).get("inputTokens", 0),
                    data.get("usage", {}).get("outputTokens", 0),
                    data.get("stopReason", "unknown"),
                    attempt + 1,
                )
                stop_reason = data.get("stopReason", "unknown")
                if stop_reason == "max_tokens":
                    _logger.warning(
                        "Bedrock output truncated (hit max_tokens). "
                        "PRD may be incomplete."
                    )
                return data["output"]["message"]["content"][0]["text"]

            # Retryable server errors
            if resp.status_code in (429, 500, 502, 503, 529):
                _kind = "THROTTLED" if resp.status_code in (429, 529) else "SERVER-ERROR"
                _logger.warning(
                    "[gohan] Bedrock bearer %s [%d] attempt=%d/3 elapsed=%.1fs "
                    "— backing off %ds: %s",
                    _kind, resp.status_code, attempt + 1,
                    time.monotonic() - _call_t0, 2 ** attempt, resp.text[:200],
                )
                last_exc = RuntimeError(f"Bedrock API error [{resp.status_code}]: {resp.text[:200]}")
                time.sleep(2 ** attempt)  # 1s, 2s, 4s
                continue

            # Non-retryable client errors
            raise RuntimeError(f"Bedrock API error [{resp.status_code}]: {resp.text[:500]}")

        except httpx.TimeoutException as exc:
            _logger.warning(
                "[gohan] Bedrock bearer TIMEOUT attempt=%d/3 elapsed=%.1fs "
                "(read_timeout=%ds): %s",
                attempt + 1, time.monotonic() - _call_t0, DEFAULT_TIMEOUT, exc,
            )
            last_exc = RuntimeError(f"Bedrock timeout: {exc}")
            time.sleep(2 ** attempt)
            continue

    _logger.error(
        "[gohan] Bedrock bearer FAILED — all 3 attempts exhausted in %.1fs",
        time.monotonic() - _call_t0,
    )
    raise last_exc or RuntimeError("Bedrock bearer: all retries exhausted")


def _get_bedrock_client(region: str, access_key_id: str = "", secret_access_key: str = ""):
    """Create boto3 bedrock-runtime client (SigV4 auth).
    If access_key_id/secret_access_key are provided, uses explicit credentials.
    Otherwise falls back to instance profile / IRSA (EKS pod role).
    """
    kwargs = {
        "service_name": "bedrock-runtime",
        "region_name": region,
        "config": BotoConfig(
            read_timeout=DEFAULT_TIMEOUT,
            connect_timeout=30,
            retries={"max_attempts": 3, "mode": "adaptive"},
        ),
    }
    if access_key_id and secret_access_key:
        kwargs["aws_access_key_id"] = access_key_id
        kwargs["aws_secret_access_key"] = secret_access_key

    return boto3.client(**kwargs)


def generate_prd(
    inference_arn: str,
    region: str,
    system_prompt: str,
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    access_key_id: str = "",
    secret_access_key: str = "",
) -> str:
    """Generate PRD text via AWS Bedrock Converse API.

    Args:
        inference_arn: Model inference profile ARN.
        region: AWS region.
        system_prompt: System prompt (prd_v9_pipeline.md content).
        messages: Conversation messages [{"role": "user"/"assistant", "content": "..."}].
        max_tokens: Max response tokens.
        temperature: Sampling temperature.
        access_key_id: Bearer token (ABSK...) or AWS access key ID.
        secret_access_key: AWS secret key (empty for bearer token mode).
    Returns:
        Generated PRD text string.
    Raises:
        RuntimeError: If API call fails.
    """
    # Bearer token mode (ABSK format)
    if _is_bearer_token(access_key_id):
        _logger.info("Using bearer token authentication for Bedrock")
        return _call_bedrock_bearer(
            inference_arn, region, access_key_id,
            system_prompt, messages, max_tokens, temperature,
        )

    # Standard SigV4 mode (boto3)
    client = _get_bedrock_client(region, access_key_id, secret_access_key)

    bedrock_messages = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            bedrock_messages.append({"role": msg["role"], "content": [{"text": content}]})
        elif isinstance(content, list):
            bedrock_messages.append({"role": msg["role"], "content": content})
        else:
            bedrock_messages.append({"role": msg["role"], "content": [{"text": str(content)}]})

    _logger.info(
        "[gohan] Calling Bedrock Converse (sigv4): model=%s region=%s "
        "messages=%d max_tokens=%d",
        inference_arn,
        region,
        len(messages),
        max_tokens,
    )

    _call_t0 = time.monotonic()
    try:
        response = client.converse(
            modelId=inference_arn,
            system=[{"text": system_prompt}],
            messages=bedrock_messages,
            inferenceConfig={
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
        )

        _logger.info(
            "[gohan] Bedrock sigv4 OK in %.1fs — input_tokens=%d "
            "output_tokens=%d stop_reason=%s",
            time.monotonic() - _call_t0,
            response.get("usage", {}).get("inputTokens", 0),
            response.get("usage", {}).get("outputTokens", 0),
            response.get("stopReason", "unknown"),
        )

        return response["output"]["message"]["content"][0]["text"]

    except ReadTimeoutError as exc:
        _logger.error(
            "[gohan] Bedrock sigv4 TIMEOUT after %.1fs (read_timeout=%ds): %s",
            time.monotonic() - _call_t0, DEFAULT_TIMEOUT, exc,
        )
        raise RuntimeError(
            f"Bedrock API timeout after {DEFAULT_TIMEOUT}s: {exc}"
        ) from exc
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "unknown")
        error_msg = exc.response.get("Error", {}).get("Message", str(exc))
        _throttle_codes = (
            "ThrottlingException", "TooManyRequestsException",
            "ServiceQuotaExceededException", "ModelTimeoutException",
        )
        _tag = "THROTTLED" if error_code in _throttle_codes else "ERROR"
        _logger.error(
            "[gohan] Bedrock sigv4 %s [%s] after %.1fs: %s",
            _tag, error_code, time.monotonic() - _call_t0, error_msg,
        )
        raise RuntimeError(
            f"Bedrock API error [{error_code}]: {error_msg}"
        ) from exc
    except Exception as exc:
        _logger.error(
            "[gohan] Bedrock sigv4 call FAILED after %.1fs: %s",
            time.monotonic() - _call_t0, exc,
        )
        raise RuntimeError(f"Bedrock API call failed: {exc}") from exc
