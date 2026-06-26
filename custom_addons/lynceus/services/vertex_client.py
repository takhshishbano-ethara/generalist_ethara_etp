from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import dataclass, field

import requests

from ..models import credential_manager
from . import cost

_logger = logging.getLogger(__name__)


_VERTEX_KEY_PARAM = "lynceus.vertex_api_key"
_SEED_PROMPT_FILE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "seed_prompts"
    / "image_gen_prompt_generator_copy_final.md"
)

DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_BASE_URL = "https://aiplatform.googleapis.com/v1/publishers/google/models"
DEFAULT_BATCH_CALL_SIZE = 20
DEFAULT_MAX_OUTPUT_TOKENS = 4000
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = (1, 3, 7)

_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
    "HARM_CATEGORY_CIVIC_INTEGRITY",
)

_seed_prompt_cache: str | None = None


@dataclass
class BatchedGenerationCall:
    count: int
    seed: str


@dataclass
class BatchedGenerationResult:
    prompts: list[str]
    usage: cost.TokenUsage
    cost_usd: float
    model: str
    requested_count: int
    candidate_tokens: int = 0
    thoughts_tokens: int = 0
    finish_reason: str | None = None
    parse_errors: list[str] = field(default_factory=list)
    raw_response: dict = field(default_factory=dict)


@dataclass
class APIConfig:
    api_key: str
    model: str
    base_url: str
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    batch_call_size: int = DEFAULT_BATCH_CALL_SIZE
    max_retries: int = DEFAULT_MAX_RETRIES
    http_session: requests.Session | None = None


def load_seed_prompt() -> str:
    global _seed_prompt_cache
    if _seed_prompt_cache is None:
        if not _SEED_PROMPT_FILE.exists():
            raise FileNotFoundError(f"Lynceus seed prompt missing: {_SEED_PROMPT_FILE}")
        _seed_prompt_cache = _SEED_PROMPT_FILE.read_text(encoding="utf-8")
    return _seed_prompt_cache


def _get_api_key(env) -> str:
    ICP = env["ir.config_parameter"].sudo()
    stored = ICP.get_param(_VERTEX_KEY_PARAM, "")
    if not stored:
        raise RuntimeError(
            "Lynceus: Vertex AI API key not configured. "
            "Set it in Settings -> Lynceus."
        )
    return credential_manager.decrypt(ICP, stored)


def _get_settings(env) -> dict:
    ICP = env["ir.config_parameter"].sudo()
    try:
        max_output_tokens = int(
            ICP.get_param("lynceus.vertex_max_output_tokens", str(DEFAULT_MAX_OUTPUT_TOKENS))
            or DEFAULT_MAX_OUTPUT_TOKENS
        )
    except (TypeError, ValueError):
        max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
    try:
        batch_call_size = int(
            ICP.get_param("lynceus.batch_call_size", str(DEFAULT_BATCH_CALL_SIZE))
            or DEFAULT_BATCH_CALL_SIZE
        )
    except (TypeError, ValueError):
        batch_call_size = DEFAULT_BATCH_CALL_SIZE
    try:
        max_retries = int(
            ICP.get_param("lynceus.vertex_max_retries", str(DEFAULT_MAX_RETRIES))
            or DEFAULT_MAX_RETRIES
        )
    except (TypeError, ValueError):
        max_retries = DEFAULT_MAX_RETRIES
    return {
        "model": ICP.get_param("lynceus.vertex_model", DEFAULT_MODEL),
        "base_url": ICP.get_param("lynceus.vertex_base_url", DEFAULT_BASE_URL),
        "max_output_tokens": max(256, max_output_tokens),
        "batch_call_size": max(1, batch_call_size),
        "max_retries": max(1, max_retries),
    }


def build_config_from_env(env) -> APIConfig:
    settings = _get_settings(env)
    return APIConfig(
        api_key=_get_api_key(env),
        model=settings["model"],
        base_url=settings["base_url"],
        max_output_tokens=settings["max_output_tokens"],
        batch_call_size=settings["batch_call_size"],
        max_retries=settings["max_retries"],
    )


def _build_response_schema(count: int) -> dict:
    return {
        "type": "OBJECT",
        "properties": {
            "prompts": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "minItems": count,
                "maxItems": count,
            }
        },
        "required": ["prompts"],
    }


def _build_user_message(count: int, seed: str) -> str:
    return (
        f"COUNT={count}\n"
        f"SEED={seed}\n"
        "\n"
        f"Return EXACTLY {count} prompts as JSON matching the supplied schema.\n"
        "JSON shape: {\"prompts\": [\"prompt 1 text\", \"prompt 2 text\", ...]}.\n"
        f"The prompts array MUST have exactly {count} entries.\n"
        "Each prompt is INDEPENDENT - choose its TIER (lean ~60/40 dense/medium),\n"
        "ARCHETYPE, PEOPLE (1-5), and 3-5 CATEGORIES uniformly at random per the\n"
        "CATEGORY ALLOCATION section. Do NOT let earlier prompts influence later ones.\n"
        "Emit prompt prose only inside each array string - no labels, headers,\n"
        "or commentary outside the JSON."
    )


def _build_safety_settings() -> list[dict]:
    return [{"category": c, "threshold": "BLOCK_NONE"} for c in _SAFETY_CATEGORIES]


def _build_payload(config: APIConfig, seed_prompt: str, call: BatchedGenerationCall) -> dict:
    return {
        "systemInstruction": {
            "parts": [{"text": seed_prompt}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": _build_user_message(call.count, call.seed)}],
            }
        ],
        "generationConfig": {
            "temperature": 0.95,
            "topP": 0.95,
            "maxOutputTokens": config.max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": _build_response_schema(call.count),
            "thinkingConfig": {"thinkingBudget": 0},
        },
        "safetySettings": _build_safety_settings(),
    }


def _redact_url(url: str) -> str:
    if "key=" in url:
        head, _, _rest = url.partition("key=")
        return head + "key=****"
    return url


def _post_generate(
    session: requests.Session | None,
    url: str,
    payload: dict,
    timeout: tuple[int, int],
    max_retries: int,
) -> dict:
    poster = session.post if session else requests.post
    headers = {"Content-Type": "application/json"}
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        _logger.debug(
            "Lynceus Vertex POST %s (attempt %d/%d)",
            _redact_url(url), attempt + 1, max_retries,
        )
        try:
            resp = poster(url, headers=headers, json=payload, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            _logger.warning(
                "Lynceus Vertex network error attempt %d: %s",
                attempt + 1, exc,
            )
            if attempt + 1 >= max_retries:
                raise RuntimeError(
                    f"Vertex AI network error after {max_retries} attempts: {exc}"
                ) from exc
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            continue

        status = resp.status_code
        if 200 <= status < 300:
            return resp.json() or {}

        body_text = resp.text[:500]

        if status in (401, 403):
            raise RuntimeError(f"Vertex AI auth failed (HTTP {status}): {body_text}")
        if status == 400:
            raise RuntimeError(f"Vertex AI validation error (HTTP {status}): {body_text}")
        if status == 402:
            raise RuntimeError(f"Vertex AI billing error (HTTP {status}): {body_text}")
        if status == 404:
            raise RuntimeError(f"Vertex AI model not found (HTTP {status}): {body_text}")
        if status == 429:
            retry_after_raw = resp.headers.get("Retry-After")
            try:
                retry_after = int(retry_after_raw) if retry_after_raw else None
            except (TypeError, ValueError):
                retry_after = None
            if attempt + 1 >= max_retries:
                raise RuntimeError(
                    f"Vertex AI rate limited after {max_retries} attempts: {body_text}"
                )
            sleep_for = (
                retry_after if retry_after is not None
                else RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            )
            time.sleep(min(int(sleep_for), 60))
            continue
        if 500 <= status < 600:
            if attempt + 1 >= max_retries:
                raise RuntimeError(
                    f"Vertex AI server error {status} after {max_retries} attempts: {body_text}"
                )
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            continue

        raise RuntimeError(f"Vertex AI unexpected HTTP {status}: {body_text}")

    raise RuntimeError(
        f"Vertex AI exhausted retries without resolution; last error: {last_exc}"
    )


def _extract_text(body: dict) -> tuple[str, str | None]:
    candidates = body.get("candidates") or []
    if not candidates:
        return "", None
    first = candidates[0] or {}
    finish_reason = first.get("finishReason")
    content = first.get("content") or {}
    parts = content.get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return text, finish_reason


def _extract_usage(body: dict, model: str) -> tuple[cost.TokenUsage, int, int, float]:
    usage_meta = body.get("usageMetadata") or {}
    prompt_tokens = int(usage_meta.get("promptTokenCount", 0))
    candidate_tokens = int(usage_meta.get("candidatesTokenCount", 0))
    thoughts_tokens = int(usage_meta.get("thoughtsTokenCount", 0) or 0)
    usage = cost.TokenUsage(
        input_tokens=prompt_tokens,
        output_tokens=candidate_tokens + thoughts_tokens,
    )
    cost_usd = cost.estimate_usd(model, usage)
    return usage, candidate_tokens, thoughts_tokens, cost_usd


def _parse_prompts_json(text: str, expected_count: int) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    if not text:
        errors.append("empty model output")
        return [], errors

    snippet = text.strip()
    data: object
    try:
        data = json.loads(snippet)
    except json.JSONDecodeError as exc:
        if snippet.startswith("{"):
            last_brace = snippet.rfind("}")
            if last_brace > 0:
                try:
                    data = json.loads(snippet[: last_brace + 1])
                    errors.append(f"json salvaged via trim (orig: {exc})")
                except json.JSONDecodeError as exc2:
                    errors.append(f"json decode failed even after trim: {exc2}")
                    return [], errors
            else:
                errors.append(f"json decode failed: {exc}")
                return [], errors
        else:
            errors.append(f"json decode failed and output not JSON-like: {exc}")
            return [], errors

    if not isinstance(data, dict):
        errors.append(f"expected JSON object, got {type(data).__name__}")
        return [], errors

    raw_prompts = data.get("prompts")
    if not isinstance(raw_prompts, list):
        errors.append("missing or non-list 'prompts' key")
        return [], errors

    prompts: list[str] = []
    for idx, item in enumerate(raw_prompts):
        if not isinstance(item, str):
            errors.append(f"item {idx} not a string ({type(item).__name__})")
            continue
        # Gemini occasionally returns NUL (0x00) bytes in adversarial prompt
        # output; Postgres rejects them in TEXT/VARCHAR columns so we strip
        # them before any downstream code touches the value.
        cleaned = item.replace("\x00", "").strip()
        if not cleaned:
            errors.append(f"item {idx} empty after sanitize+strip")
            continue
        prompts.append(cleaned)

    if len(prompts) < expected_count:
        errors.append(f"got {len(prompts)} prompts, expected {expected_count} (partial)")
    elif len(prompts) > expected_count:
        errors.append(f"got {len(prompts)} > expected {expected_count}, truncating")
        prompts = prompts[:expected_count]

    return prompts, errors


def _generate_batch_pure(
    config: APIConfig,
    seed_prompt: str,
    call: BatchedGenerationCall,
    timeout: tuple[int, int] = (30, 180),
) -> BatchedGenerationResult:
    url = (
        f"{config.base_url.rstrip('/')}/{config.model}:generateContent"
        f"?key={config.api_key}"
    )
    payload = _build_payload(config, seed_prompt, call)
    body = _post_generate(config.http_session, url, payload, timeout, config.max_retries)

    text, finish_reason = _extract_text(body)
    usage, candidate_tokens, thoughts_tokens, cost_usd = _extract_usage(body, config.model)

    parse_errors: list[str] = []
    if finish_reason and finish_reason != "STOP":
        parse_errors.append(f"non-STOP finish reason: {finish_reason}")

    if finish_reason == "SAFETY":
        return BatchedGenerationResult(
            prompts=[],
            usage=usage,
            cost_usd=cost_usd,
            model=config.model,
            requested_count=call.count,
            candidate_tokens=candidate_tokens,
            thoughts_tokens=thoughts_tokens,
            finish_reason=finish_reason,
            parse_errors=parse_errors,
            raw_response=body,
        )

    prompts, prompt_parse_errors = _parse_prompts_json(text, call.count)
    parse_errors.extend(prompt_parse_errors)

    return BatchedGenerationResult(
        prompts=prompts,
        usage=usage,
        cost_usd=cost_usd,
        model=config.model,
        requested_count=call.count,
        candidate_tokens=candidate_tokens,
        thoughts_tokens=thoughts_tokens,
        finish_reason=finish_reason,
        parse_errors=parse_errors,
        raw_response=body,
    )


def generate_batch(
    env,
    call: BatchedGenerationCall,
    timeout: tuple[int, int] = (30, 180),
) -> BatchedGenerationResult:
    config = build_config_from_env(env)
    return _generate_batch_pure(config, load_seed_prompt(), call, timeout)
