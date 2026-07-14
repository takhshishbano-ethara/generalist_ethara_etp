"""OpenAI-compatible chat-completions client for Ethara HRMS resume screening.

Pure-Python HTTP client (no Odoo imports) targeting any OpenAI-compatible
``/chat/completions`` endpoint. Works with OpenRouter, Groq, direct OpenAI,
Google AI Studio and any other gateway that speaks the OpenAI protocol.

Behaviour:
- 3-attempt exponential backoff (1s, 3s, 7s) on network errors / 5xx,
- 429 handling that honours ``Retry-After`` (capped at 60s),
- Authorization header redacted in log lines.
"""

from __future__ import annotations

import logging
import time

import requests

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LLM_MODEL = "openai/gpt-oss-120b"
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = (1, 3, 7)

_logger = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, message, *, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class LLMAuthError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    def __init__(self, message, *, status_code=None, body=None, retry_after=None):
        super().__init__(message, status_code=status_code, body=body)
        self.retry_after = retry_after


class LLMTimeoutError(LLMError):
    pass


class LLMAPIError(LLMError):
    pass


def _headers(api_key, *, http_referer=None, app_title=None):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if http_referer:
        headers["HTTP-Referer"] = http_referer
    if app_title:
        headers["X-Title"] = app_title
    return headers


def _redact_headers(headers):
    out = dict(headers or {})
    if "Authorization" in out:
        out["Authorization"] = "Bearer ****"
    return out


def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return None


def _backoff(attempt):
    return RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]


def _request(method, url, *, headers, json_body, timeout, max_retries):
    last_exc = None
    for attempt in range(max_retries):
        _logger.info(
            "Ethara HRMS LLM %s %s (attempt %d/%d) headers=%s",
            method, url, attempt + 1, max_retries, _redact_headers(headers),
        )
        try:
            response = requests.request(
                method, url, headers=headers, json=json_body, timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            _logger.warning(
                "Ethara HRMS LLM network error on attempt %d: %s",
                attempt + 1, exc,
            )
            if attempt + 1 >= max_retries:
                raise LLMTimeoutError(
                    f"Network error after {max_retries} attempts: {exc}",
                ) from exc
            time.sleep(_backoff(attempt))
            continue

        status = response.status_code
        body = _safe_json(response)

        if 200 <= status < 300:
            return body or {}
        if status in (401, 403):
            raise LLMAuthError(
                f"Authentication failed ({status})",
                status_code=status, body=body,
            )
        if status == 400:
            raise LLMAPIError(
                f"Validation error: {body}", status_code=status, body=body,
            )
        if status == 402:
            raise LLMAPIError(
                f"Billing error ({status}): {body}",
                status_code=status, body=body,
            )
        if status == 429:
            retry_after_raw = response.headers.get("Retry-After")
            try:
                retry_after = int(retry_after_raw) if retry_after_raw else None
            except (TypeError, ValueError):
                retry_after = None
            if attempt + 1 >= max_retries:
                raise LLMRateLimitError(
                    f"Rate limited after {max_retries} attempts",
                    status_code=status, body=body, retry_after=retry_after,
                )
            sleep_for = retry_after if retry_after is not None else _backoff(attempt)
            time.sleep(min(int(sleep_for), 60))
            continue
        if 500 <= status < 600:
            if attempt + 1 >= max_retries:
                raise LLMAPIError(
                    f"Server error {status} after {max_retries} attempts: {body}",
                    status_code=status, body=body,
                )
            time.sleep(_backoff(attempt))
            continue

        raise LLMAPIError(
            f"Unexpected status {status}: {body}",
            status_code=status, body=body,
        )

    raise LLMAPIError(
        f"Exhausted retries without resolution; last error: {last_exc}",
    )


def _extract_content(body):
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMAPIError(
            f"Malformed completion response: {body}", body=body,
        )
    if not isinstance(content, str) or not content.strip():
        raise LLMAPIError("Completion response had empty content", body=body)
    return content


def chat_completion(
    api_key,
    *,
    model,
    system_prompt,
    user_text,
    base_url=DEFAULT_BASE_URL,
    temperature=0.0,
    timeout=DEFAULT_TIMEOUT,
    max_retries=DEFAULT_MAX_RETRIES,
    http_referer=None,
    app_title="Ethara HRMS",
):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    # OpenRouter extension: return cost alongside token counts. Strict
    # gateways (e.g. Groq) reject unknown top-level keys, so only send
    # when routing via openrouter.ai.
    if "openrouter.ai" in (base_url or ""):
        payload["usage"] = {"include": True}

    headers = _headers(api_key, http_referer=http_referer, app_title=app_title)
    started = time.monotonic()
    body = _request(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        headers=headers,
        json_body=payload,
        timeout=timeout,
        max_retries=max_retries,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    content = _extract_content(body)
    usage = body.get("usage") or {}
    cost = usage.get("cost")
    try:
        cost_usd = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_usd = None

    return {
        "content": content,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cost_usd": cost_usd,
        "model": body.get("model") or model,
        "latency_ms": latency_ms,
        "raw": body,
    }
