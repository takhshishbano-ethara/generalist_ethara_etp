"""AWS Bedrock Converse API client for PRD generation.

Supports two authentication modes:
1. Bearer token (ABSK format) — for application inference profiles
2. SigV4 (IAM credentials) — standard boto3 auth (instance profile/IRSA/explicit keys)
"""

import json
import logging
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
        bedrock_messages.append({
            "role": msg["role"],
            "content": [{"text": msg["content"]}],
        })

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
        "Calling Bedrock Converse (bearer): model=%s, region=%s, messages=%d, max_tokens=%d",
        inference_arn,
        region,
        len(messages),
        max_tokens,
    )

    last_exc = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=30, read=DEFAULT_TIMEOUT, write=30, pool=30)) as client:
                resp = client.post(endpoint, json=payload, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                _logger.info(
                    "Bedrock response: input_tokens=%d, output_tokens=%d, stop_reason=%s",
                    data.get("usage", {}).get("inputTokens", 0),
                    data.get("usage", {}).get("outputTokens", 0),
                    data.get("stopReason", "unknown"),
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
                _logger.warning(
                    "Bedrock bearer API [%d] (attempt %d/3): %s",
                    resp.status_code, attempt + 1, resp.text[:200],
                )
                last_exc = RuntimeError(f"Bedrock API error [{resp.status_code}]: {resp.text[:200]}")
                import time as _time
                _time.sleep(2 ** attempt)  # 1s, 2s, 4s
                continue

            # Non-retryable client errors
            raise RuntimeError(f"Bedrock API error [{resp.status_code}]: {resp.text[:500]}")

        except httpx.TimeoutException as exc:
            _logger.warning("Bedrock bearer timeout (attempt %d/3): %s", attempt + 1, exc)
            last_exc = RuntimeError(f"Bedrock timeout: {exc}")
            import time as _time
            _time.sleep(2 ** attempt)
            continue

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
        system_prompt: System prompt (prd_agent_spec.md content).
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
        bedrock_messages.append({
            "role": msg["role"],
            "content": [{"text": msg["content"]}],
        })

    _logger.info(
        "Calling Bedrock Converse: model=%s, region=%s, messages=%d, max_tokens=%d",
        inference_arn,
        region,
        len(messages),
        max_tokens,
    )

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
            "Bedrock response: input_tokens=%d, output_tokens=%d, stop_reason=%s",
            response.get("usage", {}).get("inputTokens", 0),
            response.get("usage", {}).get("outputTokens", 0),
            response.get("stopReason", "unknown"),
        )

        return response["output"]["message"]["content"][0]["text"]

    except ReadTimeoutError as exc:
        _logger.error("Bedrock API timeout after %ds: %s", DEFAULT_TIMEOUT, exc)
        raise RuntimeError(
            f"Bedrock API timeout after {DEFAULT_TIMEOUT}s: {exc}"
        ) from exc
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "unknown")
        error_msg = exc.response.get("Error", {}).get("Message", str(exc))
        _logger.error("Bedrock API error [%s]: %s", error_code, error_msg)
        raise RuntimeError(
            f"Bedrock API error [{error_code}]: {error_msg}"
        ) from exc
    except Exception as exc:
        _logger.error("Bedrock API call failed: %s", exc)
        raise RuntimeError(f"Bedrock API call failed: {exc}") from exc
