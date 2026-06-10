from __future__ import annotations

import logging
import time

import requests


BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = (1, 3, 7)

_logger = logging.getLogger(__name__)


class OpenRouterError(Exception):
    def __init__(self, message, *, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class OpenRouterAuthError(OpenRouterError):
    pass


class OpenRouterRateLimitError(OpenRouterError):
    def __init__(self, message, *, status_code=None, body=None, retry_after=None):
        super().__init__(message, status_code=status_code, body=body)
        self.retry_after = retry_after


class OpenRouterValidationError(OpenRouterError):
    pass


class OpenRouterAPIError(OpenRouterError):
    pass


class OpenRouterTimeoutError(OpenRouterError):
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


def _request(method, url, *, headers, json_body, timeout, max_retries):
    last_exc = None
    for attempt in range(max_retries):
        _logger.info(
            "OpenRouter %s %s (attempt %d/%d) headers=%s",
            method, url, attempt + 1, max_retries, _redact_headers(headers),
        )
        try:
            response = requests.request(
                method, url, headers=headers, json=json_body, timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            _logger.warning(
                "OpenRouter %s %s network error on attempt %d: %s",
                method, url, attempt + 1, exc,
            )
            if attempt + 1 >= max_retries:
                raise OpenRouterTimeoutError(
                    f"Network error after {max_retries} attempts: {exc}",
                ) from exc
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            continue

        status = response.status_code
        body = _safe_json(response)

        if 200 <= status < 300:
            return body or {}

        if status in (401, 403):
            raise OpenRouterAuthError(
                f"Authentication failed ({status})",
                status_code=status, body=body,
            )
        if status == 400:
            raise OpenRouterValidationError(
                f"Validation error: {body}", status_code=status, body=body,
            )
        if status == 402:
            raise OpenRouterAPIError(
                f"Billing error ({status}): {body}", status_code=status, body=body,
            )
        if status == 429:
            retry_after_raw = response.headers.get("Retry-After")
            try:
                retry_after = int(retry_after_raw) if retry_after_raw else None
            except (TypeError, ValueError):
                retry_after = None
            if attempt + 1 >= max_retries:
                raise OpenRouterRateLimitError(
                    f"Rate limited after {max_retries} attempts",
                    status_code=status, body=body, retry_after=retry_after,
                )
            sleep_for = retry_after if retry_after is not None else RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            time.sleep(min(int(sleep_for), 60))
            continue
        if 500 <= status < 600:
            if attempt + 1 >= max_retries:
                raise OpenRouterAPIError(
                    f"Server error {status} after {max_retries} attempts: {body}",
                    status_code=status, body=body,
                )
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            continue

        raise OpenRouterAPIError(
            f"Unexpected status {status}: {body}", status_code=status, body=body,
        )

    raise OpenRouterAPIError(
        f"Exhausted retries without resolution; last error: {last_exc}",
    )


def chat_completion_vision(
    api_key,
    *,
    model,
    system_prompt,
    user_text,
    image_urls,
    response_format=None,
    temperature=0.0,
    http_referer=None,
    app_title=None,
    max_retries=DEFAULT_MAX_RETRIES,
    timeout=DEFAULT_TIMEOUT,
):
    content_blocks = [{"type": "text", "text": user_text}]
    for url in image_urls:
        if url:
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": url},
            })
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": content_blocks})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    headers = _headers(api_key, http_referer=http_referer, app_title=app_title)
    return _request(
        "POST",
        f"{BASE_URL}/chat/completions",
        headers=headers,
        json_body=payload,
        timeout=timeout,
        max_retries=max_retries,
    )
