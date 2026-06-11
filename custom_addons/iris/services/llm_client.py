"""Provider-agnostic LLM chat-completions client for Iris.

Pure-Python HTTP client (no Odoo imports) targeting any OpenAI-compatible
``/chat/completions`` endpoint. The default base URL is OpenRouter, which
fronts Anthropic / Kimi / Gemini behind one API, but any compatible gateway
works by overriding ``base_url``.

Adapted from ``i2i/services/openrouter_client.py``:

- typed exception hierarchy (renamed with an ``LLM*`` prefix),
- 3-attempt exponential backoff (1s, 3s, 7s) on network errors / 5xx,
- 429 handling that honours the ``Retry-After`` header (capped at 60s),
- Authorization header redacted in all log lines.

Iris-specific additions:

- text-only ``chat_completion()`` (system + user message),
- ``{"usage": {"include": true}}`` in the payload so OpenRouter returns the
  request cost alongside token counts,
- normalised return dict with content, token usage, cost and latency.
"""

from __future__ import annotations

import logging
import time

import requests

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = (1, 3, 7)

_logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Base error for all LLM client failures."""

    def __init__(self, message, *, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class LLMAuthError(LLMError):
    """401/403 — invalid or missing API key. Never retried."""


class LLMRateLimitError(LLMError):
    """429 — rate limited even after exhausting retries."""

    def __init__(self, message, *, status_code=None, body=None, retry_after=None):
        super().__init__(message, status_code=status_code, body=body)
        self.retry_after = retry_after


class LLMTimeoutError(LLMError):
    """Network-level timeout / connection failure after exhausting retries."""


class LLMAPIError(LLMError):
    """Any other non-success API response (400, 402, 5xx, unexpected)."""


def _headers(api_key, *, http_referer=None, app_title=None):
    """Build request headers; optional OpenRouter attribution headers."""
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
    """Return a copy of ``headers`` with the bearer token masked for logging."""
    out = dict(headers or {})
    if "Authorization" in out:
        out["Authorization"] = "Bearer ****"
    return out


def _safe_json(response):
    """Parse the response body as JSON, returning ``None`` on failure."""
    try:
        return response.json()
    except ValueError:
        return None


def _backoff(attempt):
    """Sleep duration (seconds) for a given zero-based attempt index."""
    return RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]


def _request(method, url, *, headers, json_body, timeout, max_retries):
    """Issue an HTTP request with retry/backoff and typed error mapping.

    Retries (up to ``max_retries`` total attempts) on network errors, 429
    (honouring ``Retry-After``, capped at 60s) and 5xx. Raises immediately
    on 401/403 (auth), 400 (validation) and 402 (billing).
    """
    last_exc = None
    for attempt in range(max_retries):
        _logger.info(
            "Iris LLM %s %s (attempt %d/%d) headers=%s",
            method, url, attempt + 1, max_retries, _redact_headers(headers),
        )
        try:
            response = requests.request(
                method, url, headers=headers, json=json_body, timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            _logger.warning(
                "Iris LLM %s %s network error on attempt %d: %s",
                method, url, attempt + 1, exc,
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
                f"Billing error ({status}): {body}", status_code=status, body=body,
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
            f"Unexpected status {status}: {body}", status_code=status, body=body,
        )

    raise LLMAPIError(
        f"Exhausted retries without resolution; last error: {last_exc}",
    )


def _extract_content(body):
    """Pull the assistant message text out of a chat-completions body."""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMAPIError(
            f"Malformed completion response (no choices[0].message.content): {body}",
            body=body,
        )
    if not isinstance(content, str) or not content.strip():
        raise LLMAPIError(
            "Completion response contained empty content", body=body,
        )
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
    app_title="Ethara Iris",
):
    """Run a single text chat completion and return a normalised result.

    Args:
        api_key: Bearer token for the gateway (e.g. OpenRouter key).
        model: Model slug, e.g. ``anthropic/claude-sonnet-4.5``.
        system_prompt: System message content (may be empty/None to omit).
        user_text: User message content.
        base_url: API root; ``{base_url}/chat/completions`` is POSTed.
        temperature: Sampling temperature (default 0.0 — deterministic).
        timeout: Per-request timeout in seconds.
        max_retries: Total attempts for retryable failures.
        http_referer: Optional ``HTTP-Referer`` attribution header.
        app_title: Optional ``X-Title`` attribution header.

    Returns:
        dict with keys:
            ``content`` (str): assistant message text.
            ``prompt_tokens`` / ``completion_tokens`` (int): token usage.
            ``cost_usd`` (float | None): request cost when the gateway
                reports it (OpenRouter ``usage.include``), else ``None``.
            ``model`` (str): model actually used (as echoed by the API).
            ``latency_ms`` (int): wall-clock round-trip including retries.
            ``raw`` (dict): full decoded response body.

    Raises:
        LLMAuthError: invalid/missing key (401/403) — not retried.
        LLMRateLimitError: 429 after exhausting retries.
        LLMTimeoutError: network failure after exhausting retries.
        LLMAPIError: validation/billing/server errors or malformed body.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        # OpenRouter extension: return cost alongside token counts.
        # Harmless on other OpenAI-compatible gateways (unknown keys ignored).
        "usage": {"include": True},
    }

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
