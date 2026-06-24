from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from ..models import credential_manager
from . import cost
from .anthropic_client import GenerationCall, GenerationResult, load_seed_prompt, _build_user_message

_logger = logging.getLogger(__name__)


_OPENROUTER_KEY_PARAM = "lynceus.openrouter_api_key"

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemini-3.5-flash"
DEFAULT_APP_TITLE = "Ethara Lynceus"
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = (1, 3, 7)


@dataclass
class APIConfig:
    api_key: str
    model: str
    base_url: str
    http_referer: str | None = None
    app_title: str | None = None
    max_retries: int = DEFAULT_MAX_RETRIES
    max_tokens: int = 300
    http_session: requests.Session | None = None


def _get_api_key(env) -> str:
    ICP = env["ir.config_parameter"].sudo()
    stored = ICP.get_param(_OPENROUTER_KEY_PARAM, "")
    if not stored:
        raise RuntimeError(
            "Lynceus: OpenRouter API key not configured. "
            "Set it in Settings -> Lynceus."
        )
    return credential_manager.decrypt(ICP, stored)


def _get_settings(env) -> dict:
    ICP = env["ir.config_parameter"].sudo()
    try:
        max_retries = int(ICP.get_param("lynceus.openrouter_max_retries", str(DEFAULT_MAX_RETRIES)))
    except (TypeError, ValueError):
        max_retries = DEFAULT_MAX_RETRIES
    try:
        max_tokens = int(ICP.get_param("lynceus.max_tokens_per_call", "300") or "300")
    except (TypeError, ValueError):
        max_tokens = 300
    return {
        "model": ICP.get_param("lynceus.openrouter_model", DEFAULT_MODEL),
        "base_url": ICP.get_param("lynceus.openrouter_base_url", DEFAULT_BASE_URL),
        "http_referer": ICP.get_param("lynceus.openrouter_http_referer", "") or None,
        "app_title": ICP.get_param("lynceus.openrouter_app_title", DEFAULT_APP_TITLE) or None,
        "max_retries": max(1, max_retries),
        "max_tokens": max_tokens,
    }


def build_config_from_env(env) -> APIConfig:
    settings = _get_settings(env)
    return APIConfig(
        api_key=_get_api_key(env),
        model=settings["model"],
        base_url=settings["base_url"],
        http_referer=settings["http_referer"],
        app_title=settings["app_title"],
        max_retries=settings["max_retries"],
        max_tokens=settings["max_tokens"],
    )


def _build_headers(api_key: str, *, http_referer=None, app_title=None) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if http_referer:
        headers["HTTP-Referer"] = http_referer
    if app_title:
        headers["X-Title"] = app_title
    return headers


def _redact_headers(headers: dict) -> dict:
    out = dict(headers or {})
    if "Authorization" in out:
        out["Authorization"] = "Bearer ****"
    return out


def _post_chat(
    session: requests.Session | None,
    url: str,
    headers: dict,
    payload: dict,
    timeout: tuple[int, int],
    max_retries: int,
) -> dict:
    poster = session.post if session else requests.post
    last_exc = None
    for attempt in range(max_retries):
        _logger.debug(
            "Lynceus OpenRouter POST %s (attempt %d/%d) headers=%s",
            url, attempt + 1, max_retries, _redact_headers(headers),
        )
        try:
            resp = poster(url, headers=headers, json=payload, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            _logger.warning(
                "Lynceus OpenRouter network error attempt %d: %s",
                attempt + 1, exc,
            )
            if attempt + 1 >= max_retries:
                raise RuntimeError(
                    f"OpenRouter network error after {max_retries} attempts: {exc}"
                ) from exc
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            continue

        status = resp.status_code
        if 200 <= status < 300:
            return resp.json() or {}

        body_text = resp.text[:500]

        if status in (401, 403):
            raise RuntimeError(f"OpenRouter auth failed (HTTP {status}): {body_text}")
        if status == 400:
            raise RuntimeError(f"OpenRouter validation error (HTTP {status}): {body_text}")
        if status == 402:
            raise RuntimeError(f"OpenRouter billing error (HTTP {status}): {body_text}")
        if status == 429:
            retry_after_raw = resp.headers.get("Retry-After")
            try:
                retry_after = int(retry_after_raw) if retry_after_raw else None
            except (TypeError, ValueError):
                retry_after = None
            if attempt + 1 >= max_retries:
                raise RuntimeError(
                    f"OpenRouter rate limited after {max_retries} attempts: {body_text}"
                )
            sleep_for = retry_after if retry_after is not None else RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            time.sleep(min(int(sleep_for), 60))
            continue
        if 500 <= status < 600:
            if attempt + 1 >= max_retries:
                raise RuntimeError(
                    f"OpenRouter server error {status} after {max_retries} attempts: {body_text}"
                )
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            continue

        raise RuntimeError(f"OpenRouter unexpected HTTP {status}: {body_text}")

    raise RuntimeError(
        f"OpenRouter exhausted retries without resolution; last error: {last_exc}"
    )


def _extract_content(body: dict) -> str:
    try:
        choices = body.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return (message.get("content") or "").strip()
    except (AttributeError, IndexError, TypeError):
        return ""


def _generate_one_pure(
    config: APIConfig,
    seed_prompt: str,
    call: GenerationCall,
    timeout: tuple[int, int] = (15, 60),
) -> GenerationResult:
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    headers = _build_headers(
        config.api_key,
        http_referer=config.http_referer,
        app_title=config.app_title,
    )

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": seed_prompt},
            {"role": "user", "content": _build_user_message(call)},
        ],
        "max_tokens": config.max_tokens,
        "temperature": 0.7,
    }

    body = _post_chat(config.http_session, url, headers, payload, timeout, config.max_retries)

    text = _extract_content(body)
    if not text:
        raise RuntimeError(f"OpenRouter returned empty content: {str(body)[:500]}")

    usage_obj = body.get("usage") or {}
    usage = cost.TokenUsage(
        input_tokens=int(usage_obj.get("prompt_tokens", 0)),
        output_tokens=int(usage_obj.get("completion_tokens", 0)),
    )

    return GenerationResult(
        content=text,
        usage=usage,
        cost_usd=cost.estimate_usd(config.model, usage),
        model=config.model,
    )


def generate_one(env, call: GenerationCall, timeout: tuple[int, int] = (15, 60)) -> GenerationResult:
    config = build_config_from_env(env)
    return _generate_one_pure(config, load_seed_prompt(), call, timeout)
