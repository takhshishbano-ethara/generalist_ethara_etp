"""Pure-Python OpenRouter Seedance 2.0 video API client.

No `odoo` imports — safe to call from background threads or standalone tests.
"""

import logging
import time

import requests


BASE_URL = "https://openrouter.ai/api/v1"
SUBMIT_TIMEOUT = 30
POLL_TIMEOUT = 30
MAX_RETRIES = 3
# Backoff seconds for attempts 1, 2, 3 (used between retries).
RETRY_BACKOFF_SECONDS = (1, 3, 7)

_logger = logging.getLogger(__name__)


class OpenRouterError(Exception):
    def __init__(self, message, *, status_code=None, body=None, job_id=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.job_id = job_id


class OpenRouterAuthError(OpenRouterError):
    pass


class OpenRouterRateLimitError(OpenRouterError):
    def __init__(self, message, *, status_code=None, body=None, job_id=None, retry_after=None):
        super().__init__(message, status_code=status_code, body=body, job_id=job_id)
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
    redacted = dict(headers or {})
    if "Authorization" in redacted:
        redacted["Authorization"] = "Bearer ****"
    return redacted


def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return None


def _request(method, url, *, headers, json_body=None, timeout, job_id=None):
    last_exc = None
    for attempt in range(MAX_RETRIES):
        _logger.info(
            "OpenRouter %s %s (attempt %d/%d) headers=%s",
            method,
            url,
            attempt + 1,
            MAX_RETRIES,
            _redact_headers(headers),
        )
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            _logger.warning(
                "OpenRouter %s %s network error on attempt %d: %s",
                method,
                url,
                attempt + 1,
                exc,
            )
            if attempt + 1 >= MAX_RETRIES:
                raise OpenRouterTimeoutError(
                    f"Network error after {MAX_RETRIES} attempts: {exc}",
                    job_id=job_id,
                ) from exc
            time.sleep(RETRY_BACKOFF_SECONDS[attempt])
            continue

        status = response.status_code
        body = _safe_json(response)

        if 200 <= status < 300:
            if body is None:
                return {}
            return body

        if status in (401, 403):
            raise OpenRouterAuthError(
                f"Authentication failed ({status})",
                status_code=status,
                body=body,
                job_id=job_id,
            )

        if status == 400:
            raise OpenRouterValidationError(
                f"Validation error: {body}",
                status_code=status,
                body=body,
                job_id=job_id,
            )

        if status == 402:
            # Billing/credits — not retryable.
            raise OpenRouterAPIError(
                f"Billing error ({status}): {body}",
                status_code=status,
                body=body,
                job_id=job_id,
            )

        if status == 429:
            retry_after_raw = response.headers.get("Retry-After")
            try:
                retry_after = int(retry_after_raw) if retry_after_raw is not None else None
            except (TypeError, ValueError):
                retry_after = None
            if attempt + 1 >= MAX_RETRIES:
                raise OpenRouterRateLimitError(
                    f"Rate limited after {MAX_RETRIES} attempts",
                    status_code=status,
                    body=body,
                    job_id=job_id,
                    retry_after=retry_after,
                )
            sleep_for = retry_after if retry_after is not None else RETRY_BACKOFF_SECONDS[attempt]
            sleep_for = min(int(sleep_for), 60)
            _logger.warning(
                "OpenRouter %s %s rate-limited; sleeping %ds before retry",
                method,
                url,
                sleep_for,
            )
            time.sleep(sleep_for)
            continue

        if 500 <= status < 600:
            if attempt + 1 >= MAX_RETRIES:
                raise OpenRouterAPIError(
                    f"Server error {status} after {MAX_RETRIES} attempts: {body}",
                    status_code=status,
                    body=body,
                    job_id=job_id,
                )
            _logger.warning(
                "OpenRouter %s %s server error %d; sleeping %ds before retry",
                method,
                url,
                status,
                RETRY_BACKOFF_SECONDS[attempt],
            )
            time.sleep(RETRY_BACKOFF_SECONDS[attempt])
            continue

        # Other non-retryable 4xx.
        raise OpenRouterAPIError(
            f"Unexpected status {status}: {body}",
            status_code=status,
            body=body,
            job_id=job_id,
        )

    # Should be unreachable — loop either returns or raises.
    raise OpenRouterAPIError(
        f"Exhausted retries without resolution; last error: {last_exc}",
        job_id=job_id,
    )


def submit_video(
    api_key,
    *,
    prompt,
    duration,
    resolution,
    aspect_ratio,
    negative_prompt=None,
    seed=None,
    generate_audio=True,
    model="bytedance/seedance-2.0",
    http_referer=None,
    app_title=None,
):
    """POST /videos. Returns {"id": str, "polling_url": str|None, "status": str}."""
    payload = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "negative_prompt": negative_prompt,
        "seed": seed,
        "generate_audio": generate_audio,
    }
    # Drop None-valued keys so callers can pass optional fields cleanly.
    payload = {k: v for k, v in payload.items() if v is not None}

    headers = _headers(api_key, http_referer=http_referer, app_title=app_title)
    body = _request(
        "POST",
        f"{BASE_URL}/videos",
        headers=headers,
        json_body=payload,
        timeout=SUBMIT_TIMEOUT,
    )
    return {
        "id": body.get("id"),
        "polling_url": body.get("polling_url"),
        "status": body.get("status"),
    }


def poll_status(
    api_key,
    job_id,
    polling_url=None,
    *,
    http_referer=None,
    app_title=None,
):
    """GET /videos/{id} or polling_url. Returns full JSON body."""
    url = polling_url if polling_url else f"{BASE_URL}/videos/{job_id}"
    headers = _headers(api_key, http_referer=http_referer, app_title=app_title)
    return _request(
        "GET",
        url,
        headers=headers,
        timeout=POLL_TIMEOUT,
        job_id=job_id,
    )


def cancel_job(
    api_key,
    job_id,
    *,
    http_referer=None,
    app_title=None,
):
    """DELETE /videos/{id}. Best-effort; returns {} on 404."""
    url = f"{BASE_URL}/videos/{job_id}"
    headers = _headers(api_key, http_referer=http_referer, app_title=app_title)
    try:
        return _request(
            "DELETE",
            url,
            headers=headers,
            timeout=SUBMIT_TIMEOUT,
            job_id=job_id,
        )
    except OpenRouterAPIError as exc:
        if exc.status_code == 404:
            return {}
        raise
