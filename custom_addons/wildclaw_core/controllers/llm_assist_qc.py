import json
import logging
from typing import Optional, Tuple

_logger = logging.getLogger(__name__)


def _bedrock_client(region: str):
    try:
        import boto3
    except ImportError:
        raise RuntimeError("boto3 required for Bedrock LLM-as-judge; pip install boto3")
    return boto3.client("bedrock-runtime", region_name=region)


def call_bedrock_converse(
    inference_arn: str,
    region: str,
    system_prompt: str,
    user_message: str,
    *,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    timeout: int = 120,
) -> Tuple[str, dict]:
    client = _bedrock_client(region)
    response = client.converse(
        modelId=inference_arn,
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        system=[{"text": system_prompt}] if system_prompt else None,
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    output = response.get("output", {}).get("message", {})
    text_parts = [b.get("text", "") for b in output.get("content", []) if b.get("text")]
    usage = response.get("usage", {}) or {}
    return ("".join(text_parts), {
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "total_tokens": usage.get("totalTokens", 0),
    })
