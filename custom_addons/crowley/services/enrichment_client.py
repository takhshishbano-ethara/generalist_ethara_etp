from __future__ import annotations

import logging
import os
import random
import time

_logger = logging.getLogger(__name__)

_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENHANCE_MD_PATH = os.path.join(_MODULE_ROOT, "enhance.md")

_cached_system_prompt: str | None = None

DEFAULT_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"
DEFAULT_REGION = "ap-south-1"
DEFAULT_MAX_TOKENS = 600
DEFAULT_TEMPERATURE = 0.8
DEFAULT_TOP_P = 0.9
DEFAULT_MAX_ATTEMPTS = 5

_RETRYABLE_ERROR_NAMES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelTimeoutException",
    "ModelStreamErrorException",
    "RequestTimeout",
}

_USER_TURN_KEYS = [
    ("Category", "Category"),
    ("Sub_Category", "Sub_Category"),
    ("Style", "Style"),
    ("Priority", "Priority"),
    ("Topic", "Topic"),
    ("Complexity", "Complexity"),
    ("Prompt", "Prompt"),
]


class EnrichmentError(Exception):
    pass


class EnrichmentAuthError(EnrichmentError):
    pass


class EnrichmentConfigError(EnrichmentError):
    pass


def load_system_prompt() -> str:
    global _cached_system_prompt
    if _cached_system_prompt is not None:
        return _cached_system_prompt
    try:
        with open(_ENHANCE_MD_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError as e:
        raise EnrichmentConfigError(
            f"Cannot read enhance.md at {_ENHANCE_MD_PATH}: {e}"
        ) from e
    if not content:
        raise EnrichmentConfigError(f"enhance.md is empty at {_ENHANCE_MD_PATH}")
    _cached_system_prompt = content
    return content


def build_user_turn(metadata: dict, previous_failures=None) -> str:
    lines = []
    for key, label in _USER_TURN_KEYS:
        val = (metadata.get(key) or "").strip()
        val = val.replace("\n", " ").replace("\r", " ").replace("\t", " ")
        lines.append(f"{label}: {val}")
    out = "\n".join(lines)
    if previous_failures:
        feedback = ["", "PREVIOUS_ATTEMPT_FAILURES:",
                    "Your last attempt failed the deterministic T2AV validator. "
                    "Address every issue below in your next output. Do not repeat "
                    "any of these mistakes:"]
        for f in previous_failures[:10]:
            rule = (f.get("rule") or "UNKNOWN").strip()
            msg = (f.get("message") or "").strip().replace("\n", " ")
            ev = (f.get("evidence") or "").strip().replace("\n", " ")
            line = f"- [{rule}] {msg}"
            if ev:
                line += f" (offending text from prior output: {ev[:200]!r})"
            feedback.append(line)
        out = out + "\n\n" + "\n".join(feedback)
    return out


def _is_retryable(exc: BaseException) -> bool:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code", "")
        if code in _RETRYABLE_ERROR_NAMES:
            return True
    name = exc.__class__.__name__
    if name in _RETRYABLE_ERROR_NAMES:
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def enrich(
    *,
    access_key: str,
    secret_key: str,
    region: str = DEFAULT_REGION,
    model_id: str = DEFAULT_MODEL_ID,
    metadata: dict,
    previous_failures=None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float = DEFAULT_TOP_P,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict:
    if not access_key or not secret_key:
        raise EnrichmentAuthError(
            "AWS Access Key ID and Secret Access Key required. "
            "Configure in Settings > Crowley."
        )

    try:
        import boto3
        from botocore.config import Config
    except ImportError as e:
        raise EnrichmentConfigError(
            "boto3 is required for Bedrock enrichment. "
            "Install with: pip install boto3"
        ) from e

    system_prompt = load_system_prompt()
    user_turn = build_user_turn(metadata, previous_failures=previous_failures)

    boto_config = Config(
        retries={"max_attempts": 0, "mode": "standard"},
        read_timeout=180,
        connect_timeout=10,
    )
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=boto_config,
    )

    last_exc: BaseException | None = None
    resp = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.converse(
                modelId=model_id,
                system=[{"text": system_prompt}],
                messages=[{"role": "user", "content": [{"text": user_turn}]}],
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                    "topP": top_p,
                },
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable(exc):
                raise EnrichmentError(
                    f"{exc.__class__.__name__}: {exc}"
                ) from exc
            delay = min(30.0, (2 ** attempt) + random.random())
            _logger.warning(
                "Bedrock retry %d/%d in %.1fs (%s: %s)",
                attempt, max_attempts, delay, exc.__class__.__name__, exc,
            )
            time.sleep(delay)

    if resp is None:
        raise EnrichmentError(
            f"Bedrock call exhausted after {max_attempts} attempts: {last_exc}"
        )

    try:
        blocks = resp["output"]["message"]["content"]
    except (KeyError, TypeError) as e:
        raise EnrichmentError(f"Unexpected Bedrock response shape: {resp!r}") from e

    text_parts = [b.get("text", "") for b in blocks if "text" in b]
    text = "".join(text_parts).strip()

    stop_reason = resp.get("stopReason") or ""
    usage = resp.get("usage") or {}
    request_id = (resp.get("ResponseMetadata") or {}).get("RequestId", "")
    return {
        "text": text,
        "input_tokens": int(usage.get("inputTokens") or 0),
        "output_tokens": int(usage.get("outputTokens") or 0),
        "stop_reason": stop_reason,
        "request_id": request_id,
    }
