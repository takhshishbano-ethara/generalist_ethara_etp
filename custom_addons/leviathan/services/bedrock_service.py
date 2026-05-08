"""AWS Bedrock Converse API client for PRD generation.

Uses boto3 with SigV4 authentication (IAM credentials from environment/instance profile).
Compatible with EKS pod IAM roles (IRSA) — no static API keys needed.
"""

import logging

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, ReadTimeoutError

_logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT = 300
DEFAULT_TEMPERATURE = 0.7


def _get_bedrock_client(region: str, access_key_id: str = "", secret_access_key: str = ""):
    """Create boto3 bedrock-runtime client.
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
        inference_arn: Model inference profile ARN (e.g., arn:aws:bedrock:us-east-1:...).
        region: AWS region.
        system_prompt: System prompt (prd_agent_spec.md content).
        messages: Conversation messages [{"role": "user"/"assistant", "content": "..."}].
        max_tokens: Max response tokens.
        temperature: Sampling temperature.
        access_key_id: Optional explicit AWS access key (empty = use instance profile/IRSA).
        secret_access_key: Optional explicit AWS secret key.
    Returns:
        Generated PRD text string.
    Raises:
        RuntimeError: If API call fails.
    """
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
