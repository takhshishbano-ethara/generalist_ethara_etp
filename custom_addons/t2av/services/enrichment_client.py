from __future__ import annotations

import logging
import os
import random
import time

import requests

_logger = logging.getLogger(__name__)

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENHANCE_MD_PATH = os.path.join(_MODULE_ROOT, "enhance.md")

_cached_system_prompt: str | None = None

DEFAULT_MODEL_ID = "google/gemini-3.5-flash"
DEFAULT_FALLBACK_MODELS = ("google/gemini-3.1-flash-lite", "google/gemini-2.5-flash")
DEFAULT_MAX_TOKENS = 8000
DEFAULT_TEMPERATURE = 0.8
DEFAULT_TOP_P = 1.0
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_TIMEOUT = 120

_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

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


def _build_word_budget_header(style) -> str:
    from . import validator as _validator_svc
    lo, hi = _validator_svc.word_band_for_style(style)
    target = (lo + hi) // 2
    style_label = (style or "").strip().lower() or "precise"
    return (
        "OUTPUT WORD BUDGET (HARD CONSTRAINT):\n"
        f"- Style is '{style_label}'. Produce between {lo} and {hi} words, inclusive.\n"
        f"- Aim for approximately {target} words. Stay strictly inside [{lo}, {hi}].\n"
        f"- Counts outside this band are REJECTED by the deterministic T2AV validator.\n"
        f"- The mandatory final 1920x1080 suffix sentence counts toward this total.\n"
        f"- A 'word' is any whitespace-separated token; count carefully before finalizing.\n"
        "\n"
    )


def build_user_turn(metadata: dict, previous_failures=None) -> str:
    lines = []
    for key, label in _USER_TURN_KEYS:
        val = (metadata.get(key) or "").strip()
        val = val.replace("\n", " ").replace("\r", " ").replace("\t", " ")
        lines.append(f"{label}: {val}")
    out = _build_word_budget_header(metadata.get("Style")) + "\n".join(lines)
    if previous_failures:
        from . import retry_hints
        targeted = retry_hints.build_hint(previous_failures)
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
        feedback.append("")
        feedback.append("RULE-TARGETED CORRECTIONS:")
        feedback.append(targeted)
        out = out + "\n\n" + "\n".join(feedback)
    return out


def _enrich_via_openrouter(
    *,
    api_key: str,
    model_id: str,
    fallback_models: tuple[str, ...] | list[str],
    system_prompt: str,
    user_turn: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    reasoning_effort: str,
    http_referer: str,
    app_title: str,
    max_attempts: int,
    timeout: float,
) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if http_referer:
        headers["HTTP-Referer"] = http_referer
    if app_title:
        headers["X-Title"] = app_title

    payload: dict = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_turn},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    if fallback_models:
        payload["models"] = [model_id] + list(fallback_models)
        payload["route"] = "fallback"
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}

    last_exc: BaseException | None = None
    resp = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                OPENROUTER_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if resp.status_code in (401, 403):
                raise EnrichmentAuthError(
                    f"OpenRouter API key rejected (HTTP {resp.status_code}): "
                    f"{resp.text[:500]}"
                )
            if resp.status_code == 402:
                raise EnrichmentError(
                    f"OpenRouter billing/credits error (HTTP 402): "
                    f"{resp.text[:500]}. Top up at https://openrouter.ai/credits"
                )
            if resp.status_code == 400:
                detail = resp.text[:1000]
                try:
                    j = resp.json()
                    detail = (
                        (j.get("error") or {}).get("message")
                        or j.get("message")
                        or detail
                    )
                except Exception:
                    pass
                raise EnrichmentError(
                    f"OpenRouter returned HTTP 400 (request rejected). "
                    f"OpenRouter said: {detail!r}. "
                    f"Model: {model_id}. "
                    f"maxTokens={max_tokens}, temperature={temperature}."
                )
            if resp.status_code in _RETRYABLE_STATUS_CODES:
                if attempt >= max_attempts:
                    raise EnrichmentError(
                        f"OpenRouter HTTP {resp.status_code} after "
                        f"{max_attempts} attempts: {resp.text[:500]}"
                    )
                retry_after = resp.headers.get("Retry-After")
                delay = min(60.0, (2 ** attempt) + random.random())
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except (TypeError, ValueError):
                        pass
                _logger.warning(
                    "OpenRouter enrichment retry %d/%d in %.1fs (HTTP %d)",
                    attempt, max_attempts, delay, resp.status_code,
                )
                time.sleep(delay)
                continue
            resp.raise_for_status()
            break
        except EnrichmentAuthError:
            raise
        except EnrichmentError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise EnrichmentError(
                    f"{exc.__class__.__name__}: {exc}"
                ) from exc
            delay = min(60.0, (2 ** attempt) + random.random())
            _logger.warning(
                "OpenRouter enrichment retry %d/%d in %.1fs (%s)",
                attempt, max_attempts, delay, exc.__class__.__name__,
            )
            time.sleep(delay)

    if resp is None:
        raise EnrichmentError(
            f"OpenRouter call exhausted after {max_attempts} attempts: {last_exc}"
        )

    try:
        data = resp.json()
    except ValueError as e:
        raise EnrichmentError(
            f"OpenRouter response not JSON: {resp.text[:300]!r}"
        ) from e

    if data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise EnrichmentError(f"OpenRouter returned error in body: {msg}")

    try:
        choice = data["choices"][0]
        message = choice["message"]
        text = message.get("content") or ""
        finish_reason = choice.get("finish_reason") or ""
    except (KeyError, IndexError, TypeError) as e:
        raise EnrichmentError(
            f"Unexpected OpenRouter response shape: {resp.text[:300]!r}"
        ) from e

    text = str(text).strip()

    usage = data.get("usage") or {}
    request_id = (
        resp.headers.get("X-Request-Id")
        or resp.headers.get("x-request-id")
        or data.get("id", "")
    )
    served_model = data.get("model") or model_id

    return {
        "text": text,
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "stop_reason": finish_reason,
        "request_id": request_id,
        "served_model": served_model,
    }


def enrich(
    *,
    openrouter_api_key: str = "",
    model_id: str = DEFAULT_MODEL_ID,
    fallback_models: tuple[str, ...] | list[str] = DEFAULT_FALLBACK_MODELS,
    metadata: dict,
    previous_failures=None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float = DEFAULT_TOP_P,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    http_referer: str = "",
    app_title: str = "Ethara T2AV",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    if not openrouter_api_key:
        raise EnrichmentAuthError(
            "OpenRouter auth required: configure 'OpenRouter API Key' "
            "in Settings > T2AV. Get a key at https://openrouter.ai/keys"
        )

    system_prompt = load_system_prompt()
    user_turn = build_user_turn(metadata, previous_failures=previous_failures)

    return _enrich_via_openrouter(
        api_key=openrouter_api_key,
        model_id=model_id,
        fallback_models=fallback_models,
        system_prompt=system_prompt,
        user_turn=user_turn,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        reasoning_effort=reasoning_effort,
        http_referer=http_referer,
        app_title=app_title,
        max_attempts=max_attempts,
        timeout=timeout,
    )


DEFAULT_QC_MODEL_ID = "google/gemini-3.5-flash"
DEFAULT_QC_TEMPERATURE = 0.3
DEFAULT_QC_TOP_P = 1.0
DEFAULT_QC_MAX_TOKENS = 1500
DEFAULT_QC_REASONING_EFFORT = "low"
DEFAULT_QC_MAX_ATTEMPTS = 3
DEFAULT_QC_TIMEOUT = 120

_QC_SYSTEM_PROMPT_FILENAME = "meta_qc_system_prompt.md"
_cached_qc_system_prompt: str | None = None


def load_qc_system_prompt() -> str:
    global _cached_qc_system_prompt
    if _cached_qc_system_prompt is not None:
        return _cached_qc_system_prompt
    import os
    module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(module_root, _QC_SYSTEM_PROMPT_FILENAME)
    with open(path, "r", encoding="utf-8") as fp:
        _cached_qc_system_prompt = fp.read()
    return _cached_qc_system_prompt


def build_qc_user_turn(
    *,
    meta_prompt: str,
    category: str,
    sub_category: str,
    topic: str,
    style: str,
    bad_sample: str,
    reasons,
    language: str = "english",
    complexity: str = "moderate",
) -> str:
    reasons_str = ", ".join(reasons) if reasons else "(none reported)"
    return (
        f"META_PROMPT:\n{meta_prompt or '(empty)'}\n\n"
        f"CATEGORY: {category or '(unspecified)'}\n"
        f"SUB_CATEGORY: {sub_category or '(unspecified)'}\n"
        f"TOPIC: {topic or '(unspecified)'}\n"
        f"STYLE: {style or 'casual'}\n"
        f"LANGUAGE: {language or 'english'}\n"
        f"COMPLEXITY: {complexity or 'moderate'}\n\n"
        f"BAD_SAMPLE (do NOT replicate):\n{bad_sample or '(empty)'}\n\n"
        f"DEFECT_REASONS: {reasons_str}\n\n"
        f"Generate the corrected prompt now. Output ONLY the prompt text."
    )


def enrich_qc(
    *,
    openrouter_api_key: str,
    meta_prompt: str,
    category: str,
    sub_category: str,
    topic: str,
    style: str,
    bad_sample: str,
    reasons,
    language: str = "english",
    complexity: str = "moderate",
    model_id: str = DEFAULT_QC_MODEL_ID,
    temperature: float = DEFAULT_QC_TEMPERATURE,
    top_p: float = DEFAULT_QC_TOP_P,
    max_tokens: int = DEFAULT_QC_MAX_TOKENS,
    reasoning_effort: str = DEFAULT_QC_REASONING_EFFORT,
    max_attempts: int = DEFAULT_QC_MAX_ATTEMPTS,
    http_referer: str = "",
    app_title: str = "Ethara T2AV",
    timeout: float = DEFAULT_QC_TIMEOUT,
) -> dict:
    if not openrouter_api_key:
        raise EnrichmentAuthError(
            "OpenRouter auth required for ambiguity recovery: configure "
            "'OpenRouter API Key' in Settings > T2AV."
        )

    system_prompt = load_qc_system_prompt()
    user_turn = build_qc_user_turn(
        meta_prompt=meta_prompt,
        category=category,
        sub_category=sub_category,
        topic=topic,
        style=style,
        bad_sample=bad_sample,
        reasons=reasons,
        language=language,
        complexity=complexity,
    )

    return _enrich_via_openrouter(
        api_key=openrouter_api_key,
        model_id=model_id,
        fallback_models=(),
        system_prompt=system_prompt,
        user_turn=user_turn,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        reasoning_effort=reasoning_effort,
        http_referer=http_referer,
        app_title=app_title,
        max_attempts=max_attempts,
        timeout=timeout,
    )
