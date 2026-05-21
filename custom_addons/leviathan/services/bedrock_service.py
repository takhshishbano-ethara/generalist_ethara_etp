"""AWS Bedrock Converse API client for PRD generation.

Supports two authentication modes:
1. Bearer token (ABSK format) — for application inference profiles
2. SigV4 (IAM credentials) — standard boto3 auth (instance profile/IRSA/explicit keys)
"""

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Optional

import boto3
import httpx
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, ReadTimeoutError

_logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT = 300
DEFAULT_TEMPERATURE = 0.7

# Cap internal retries to reduce Bedrock load per job. Default 2; admins
# can override via Settings UI (leviathan.bedrock_inner_retries) or env
# (LEVIATHAN_BEDROCK_INNER_RETRIES). The Settings UI value takes effect for
# NEW boto clients only — pod restart required to apply to all in-flight
# clients (boto client cache holds the BotoConfig built at first call).
_BEDROCK_INNER_RETRIES = int(os.environ.get("LEVIATHAN_BEDROCK_INNER_RETRIES", "2"))

# Per-(region, key_id, secret) bedrock-runtime client cache. Boto client
# construction loads service models from disk and is non-trivially expensive
# under high concurrency. Mirrors the proven pattern in extraction_service.
_CLIENT_LOCK = threading.Lock()
_CLIENT_CACHE: dict[tuple, Any] = {}

# In-process Bedrock concurrency cap. Pattern borrowed from
# preference_ranking/services/rate_limiter.py — a global semaphore that
# self-throttles BELOW the AWS Bedrock TPS quota so we never trigger
# adaptive-retry. Without this, N concurrent workers can all fire
# simultaneously, blow past the quota, and Bedrock's adaptive retry then
# queues each call for 5-30 minutes — that's the "Bedrock 200 but the call
# took 30 min" pattern we saw in prod.
#
# Sizing math:
#   max_concurrent ≈ TPS_quota_RPS × avg_call_duration_seconds
# Examples (avg PRD call ≈ 2-3s):
#   Bedrock default quota (5-10 RPS)  → 15-30 concurrent
#   Quota bumped to 50 RPS            → 100-150 concurrent (effectively uncapped)
#
# Default 5 is a SAFE floor — works even at default Bedrock quota. Raise via
# env once devops confirms the actual TPS quota. Per-process (not per-pid,
# not per-cluster) — each Odoo worker process has its own semaphore.
_BEDROCK_MAX_CONCURRENT = int(
    os.environ.get("LEVIATHAN_BEDROCK_MAX_CONCURRENT", "5")
)
_BEDROCK_SEMAPHORE = threading.Semaphore(_BEDROCK_MAX_CONCURRENT)

_logger.info(
    "[leviathan] Bedrock concurrency cap initialised: max_concurrent=%d "
    "(env LEVIATHAN_BEDROCK_MAX_CONCURRENT)",
    _BEDROCK_MAX_CONCURRENT,
)


def _bedrock_inflight():
    """Best-effort count of Bedrock slots currently held in THIS process.

    Reads the semaphore's private permit counter — CPython-stable and only
    used for logging, so a wrong value never affects behaviour.
    """
    try:
        return _BEDROCK_MAX_CONCURRENT - _BEDROCK_SEMAPHORE._value
    except Exception:
        return -1


@contextmanager
def _bedrock_slot(call_label="bedrock"):
    """Block until a concurrent-call slot is free. Self-throttles to stay
    under the Bedrock TPS quota so adaptive retry never fires.

    Timeout = 30 min — gives plenty of room for transient bursts; raises
    TimeoutError after that so a stuck queue surfaces instead of waiting
    forever. The bg worker's outer except catches the TimeoutError and
    surfaces it as a normal Bedrock failure.

    Every acquire/release is logged: this semaphore is the #1 suspect when
    PRD-gen jobs appear "stuck in generating" — a worker blocked here for
    minutes is alive and heartbeating but making no visible progress. The
    log lets you see the queue depth and per-call hold time directly.
    """
    wait_start = time.monotonic()
    _logger.info(
        "[leviathan] %s WAITING for Bedrock slot (in_flight=%d/%d, pid=%d)",
        call_label, _bedrock_inflight(), _BEDROCK_MAX_CONCURRENT, os.getpid(),
    )
    acquired = _BEDROCK_SEMAPHORE.acquire(timeout=1800)
    wait_seconds = time.monotonic() - wait_start
    if not acquired:
        _logger.error(
            "[leviathan] %s GAVE UP waiting for Bedrock slot after %.0fs "
            "(cap=%d) — this job will fail. The cap is badly undersized for "
            "the offered load, or PRD calls are far slower than the cap "
            "assumes.", call_label, wait_seconds, _BEDROCK_MAX_CONCURRENT,
        )
        raise TimeoutError(
            f"No Bedrock slot in 30min (cap={_BEDROCK_MAX_CONCURRENT}). "
            f"Either raise LEVIATHAN_BEDROCK_MAX_CONCURRENT or reduce "
            f"concurrent PRD-gen workers (LEVIATHAN_PRD_POOL_SIZE)."
        )
    if wait_seconds > 10:
        _logger.warning(
            "[leviathan] %s waited %.1fs for Bedrock slot (cap=%d) — "
            "concurrency cap is bound; raise LEVIATHAN_BEDROCK_MAX_CONCURRENT "
            "if your AWS quota allows",
            call_label, wait_seconds, _BEDROCK_MAX_CONCURRENT,
        )
    else:
        _logger.info(
            "[leviathan] %s ACQUIRED Bedrock slot after %.1fs "
            "(in_flight=%d/%d)", call_label, wait_seconds,
            _bedrock_inflight(), _BEDROCK_MAX_CONCURRENT,
        )
    held_start = time.monotonic()
    try:
        yield
    finally:
        _BEDROCK_SEMAPHORE.release()
        _logger.info(
            "[leviathan] %s RELEASED Bedrock slot (held %.1fs, in_flight now "
            "%d/%d)", call_label, time.monotonic() - held_start,
            _bedrock_inflight(), _BEDROCK_MAX_CONCURRENT,
        )


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
        "Calling Bedrock Converse (bearer): model=%s, region=%s, messages=%d, max_tokens=%d",
        inference_arn,
        region,
        len(messages),
        max_tokens,
    )

    last_exc = None
    for attempt in range(_BEDROCK_INNER_RETRIES):
        try:
            # Hold a concurrency slot for THIS attempt only — released
            # between retries so other workers can interleave. Prevents
            # us from blowing past Bedrock's TPS quota and triggering
            # the 5-30min adaptive-retry queue.
            with _bedrock_slot("bedrock-bearer-prd"):
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
                    "Bedrock bearer API [%d] (attempt %d/%d): %s",
                    resp.status_code, attempt + 1, _BEDROCK_INNER_RETRIES, resp.text[:200],
                )
                last_exc = RuntimeError(f"Bedrock API error [{resp.status_code}]: {resp.text[:200]}")
                import time as _time
                _time.sleep(2 ** attempt)  # 1s, 2s, ...
                continue

            # Non-retryable client errors
            raise RuntimeError(f"Bedrock API error [{resp.status_code}]: {resp.text[:500]}")

        except httpx.TimeoutException as exc:
            _logger.warning(
                "Bedrock bearer timeout (attempt %d/%d): %s",
                attempt + 1, _BEDROCK_INNER_RETRIES, exc,
            )
            last_exc = RuntimeError(f"Bedrock timeout: {exc}")
            import time as _time
            _time.sleep(2 ** attempt)
            continue

    raise last_exc or RuntimeError("Bedrock bearer: all retries exhausted")


def _get_bedrock_client(region: str, access_key_id: str = "", secret_access_key: str = ""):
    """Return a cached boto3 bedrock-runtime client (SigV4 auth).

    Cached per ``(region, access_key_id, secret_access_key)`` so that 50+
    concurrent PRD-gen workers don't each pay the boto-client construction
    cost. If credentials are not provided, falls back to instance profile
    / IRSA (EKS pod role).
    """
    cache_key = (region, access_key_id, secret_access_key)
    with _CLIENT_LOCK:
        client = _CLIENT_CACHE.get(cache_key)
        if client is not None:
            return client

        kwargs = {
            "service_name": "bedrock-runtime",
            "region_name": region,
            "config": BotoConfig(
                read_timeout=DEFAULT_TIMEOUT,
                connect_timeout=30,
                # 300 pool connections matches the extraction service —
                # gives us headroom for 250-concurrent without blocking on
                # the urllib3 pool.
                max_pool_connections=300,
                # Capped via env (default 2). With outer max_attempts=1
                # this means at most 2 API calls per job. Adaptive mode
                # keeps smart backoff between attempts.
                retries={
                    "max_attempts": _BEDROCK_INNER_RETRIES,
                    "mode": "adaptive",
                },
            ),
        }
        if access_key_id and secret_access_key:
            kwargs["aws_access_key_id"] = access_key_id
            kwargs["aws_secret_access_key"] = secret_access_key

        client = boto3.client(**kwargs)
        _CLIENT_CACHE[cache_key] = client
        return client


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
        content = msg["content"]
        if isinstance(content, str):
            bedrock_messages.append({"role": msg["role"], "content": [{"text": content}]})
        elif isinstance(content, list):
            bedrock_messages.append({"role": msg["role"], "content": content})
        else:
            bedrock_messages.append({"role": msg["role"], "content": [{"text": str(content)}]})

    _logger.info(
        "Calling Bedrock Converse: model=%s, region=%s, messages=%d, max_tokens=%d",
        inference_arn,
        region,
        len(messages),
        max_tokens,
    )

    try:
        # Hold a concurrency slot during the call so we stay under the
        # Bedrock TPS quota and adaptive-retry never fires.
        with _bedrock_slot("bedrock-sigv4-prd"):
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
