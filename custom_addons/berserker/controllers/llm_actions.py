# -*- coding: utf-8 -*-
"""
Berserker LLM actions — pointwise evaluation and QC via AWS Bedrock Converse API.

Pipeline:
  1. eval_single_response_kimi   — evaluate one model response on 6 dimensions (1-6 scale)
  2. eval_all_responses_kimi     — run 3 parallel evals (GPT, Gemini, Claude)
  3. perform_qc_checks_kimi      — AI-detection + justification grounding QC

All LLM calls go through call_kimi_sync which uses Bedrock Converse HTTP/2.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

import httpx

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared HTTP/2 client
# ---------------------------------------------------------------------------
_HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=30)

_HTTP_CLIENT = httpx.Client(
    http2=True,
    follow_redirects=True,
    timeout=httpx.Timeout(600, connect=10.0, pool=10.0),
    limits=_HTTP_LIMITS,
)

_BEDROCK_CLIENT = httpx.Client(
    http2=True,
    follow_redirects=False,
    timeout=httpx.Timeout(1200, connect=10.0, pool=10.0),
    limits=_HTTP_LIMITS,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_MAX = 30

_DEFAULT_MODEL_ARN = (
    "arn:aws:bedrock:ap-south-1:426628337772:application-inference-profile/pnymm9v4duzh"
)
_DEFAULT_CLAUDE_ARN = (
    "arn:aws:bedrock:ap-south-1:426628337772:application-inference-profile/claude-4-7"
)
_DEFAULT_BEDROCK_REGION = "ap-south-1"
_DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"
_DEFAULT_OPENAI_MODEL = "gpt-5.4"


_config_cache = {}
_config_cache_db = None
_config_cache_lock = threading.Lock()
_config_cache_ts = 0.0
_CONFIG_CACHE_TTL = 300  # 5 minutes


def _get_config(key, default=""):
    global _config_cache, _config_cache_ts
    now = time.time()

    # Check cache with TTL
    with _config_cache_lock:
        if now - _config_cache_ts < _CONFIG_CACHE_TTL and key in _config_cache:
            return _config_cache[key]

    # Try request.env first
    try:
        from odoo.http import request

        if request and request.env:
            val = request.env["ir.config_parameter"].sudo().get_param(key, default)
            result = val or default
            with _config_cache_lock:
                _config_cache[key] = result
                _config_cache_ts = now
            return result
    except Exception:
        pass

    # Fall back to direct DB access
    try:
        import odoo
        from odoo import SUPERUSER_ID, api
        from odoo.modules.registry import Registry

        db_name = _config_cache_db
        if not db_name:
            db_name = odoo.tools.config.get("db_name") or os.environ.get(
                "ODOO_DATABASE"
            )
        if not db_name:
            try:
                registries = Registry.registries
                if hasattr(registries, "d"):
                    db_names = list(registries.d.keys())
                elif hasattr(registries, "_values"):
                    db_names = list(registries._values.keys())
                else:
                    db_names = list(registries.keys())
                if db_names:
                    db_name = db_names[0]
            except Exception:
                pass
        if db_name:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                val = env["ir.config_parameter"].sudo().get_param(key, default)
                result = val or default
                with _config_cache_lock:
                    _config_cache[key] = result
                    _config_cache_ts = now
                return result
    except Exception as e:
        _logger.warning("_get_config(%s) failed: %s", key, e)
    return default


def invalidate_config_cache():
    global _config_cache, _config_cache_ts
    with _config_cache_lock:
        _config_cache = {}
        _config_cache_ts = 0.0


def _get_kimi_arn():
    return _get_config("berserker.kimi_arn", _DEFAULT_MODEL_ARN)


def _get_kimi_region():
    return _get_config("berserker.kimi_region", _DEFAULT_BEDROCK_REGION)


def _get_claude_arn():
    return _get_config("berserker.claude_arn", _DEFAULT_CLAUDE_ARN)


def _get_claude_region():
    return _get_config("berserker.claude_region", _DEFAULT_BEDROCK_REGION)


def _get_gemini_model():
    return _get_config("berserker.gemini_model", _DEFAULT_GEMINI_MODEL)


def _get_openai_model():
    return _get_config("berserker.openai_model", _DEFAULT_OPENAI_MODEL)


# ---------------------------------------------------------------------------
# Usage logging (thread-local context + fire-and-forget)
# ---------------------------------------------------------------------------
_usage_context = threading.local()


def set_usage_context(berserker_id=False, call_type=""):
    _usage_context.berserker_id = berserker_id
    _usage_context.call_type = call_type


_usage_log_buffer = []
_usage_log_lock = threading.Lock()
_USAGE_FLUSH_SIZE = 20


def _log_usage(
    provider, model_name, usage, success=True, error_message="", duration=0.0
):
    entry = {
        "provider": provider,
        "model_name": model_name or "",
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_tokens": usage.get("cached_tokens", 0),
        "call_type": getattr(_usage_context, "call_type", "") or "evaluation",
        "berserker_id": getattr(_usage_context, "berserker_id", False) or False,
        "success": success,
        "error_message": (error_message or "")[:5000],
        "duration_seconds": duration,
    }
    with _usage_log_lock:
        _usage_log_buffer.append(entry)
        should_flush = len(_usage_log_buffer) >= _USAGE_FLUSH_SIZE

    if should_flush:
        flush_usage_logs()


def flush_usage_logs():
    with _usage_log_lock:
        if not _usage_log_buffer:
            return
        batch = list(_usage_log_buffer)
        _usage_log_buffer.clear()

    try:
        from odoo import SUPERUSER_ID, api
        from odoo.modules.registry import Registry

        db_name = _config_cache_db
        if not db_name:
            return
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["berserker.usage.log"].create(batch)
    except Exception as e:
        _logger.warning("Usage log flush failed (non-critical): %s", e)


# ---------------------------------------------------------------------------
# Rate limiter — per-provider semaphores to cap concurrent API calls
# ---------------------------------------------------------------------------
_RATE_LIMITS = {
    "kimi": threading.Semaphore(int(os.getenv("BERSERKER_RATE_KIMI", "20"))),
    "claude": threading.Semaphore(int(os.getenv("BERSERKER_RATE_CLAUDE", "20"))),
    "gemini": threading.Semaphore(int(os.getenv("BERSERKER_RATE_GEMINI", "10"))),
    "openai": threading.Semaphore(int(os.getenv("BERSERKER_RATE_OPENAI", "10"))),
}

_RESPONSE_GEN_POOL = ThreadPoolExecutor(
    max_workers=10, thread_name_prefix="berserker_resp_gen"
)


# ---------------------------------------------------------------------------
# System prompt loader — reads .md files from system_prompts/
# ---------------------------------------------------------------------------
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "system_prompts"


def _load_prompt(filename):
    """Load a system prompt from a .md file in system_prompts/."""
    filepath = _PROMPTS_DIR / filename
    if not filepath.exists():
        _logger.warning("System prompt file not found: %s", filepath)
        return ""
    return filepath.read_text(encoding="utf-8").strip()


# Load all rubric files and assemble the combined evaluation rubrics
_RUBRICS_INTRO = _load_prompt("rubrics_intro.md")
_RUBRIC_INSTRUCTION_FOLLOWING = _load_prompt("rubric_instruction_following.md")
_RUBRIC_TRUTHFULNESS = _load_prompt("rubric_truthfulness.md")
_RUBRIC_PROMPT_CORRECTNESS = _load_prompt("rubric_prompt_correctness.md")
_RUBRIC_WRITING_STYLE = _load_prompt("rubric_writing_style.md")
_RUBRIC_VERBOSITY = _load_prompt("rubric_verbosity.md")
_RUBRIC_OVERALL_QUALITY = _load_prompt("rubric_overall_quality.md")

EVALUATION_RUBRICS = "\n\n".join(
    [
        _RUBRICS_INTRO,
        _RUBRIC_INSTRUCTION_FOLLOWING,
        _RUBRIC_TRUTHFULNESS,
        _RUBRIC_PROMPT_CORRECTNESS,
        _RUBRIC_WRITING_STYLE,
        _RUBRIC_VERBOSITY,
        _RUBRIC_OVERALL_QUALITY,
    ]
)

# Load evaluation and QC system prompts (with rubric substitution)
_EVAL_TEMPLATE = _load_prompt("evaluation.md")
BERSERKER_EVAL_SYSTEM_PROMPT = _EVAL_TEMPLATE.replace(
    "{EVALUATION_RUBRICS}", EVALUATION_RUBRICS
)

BERSERKER_QC_SYSTEM_PROMPT = _load_prompt("qc.md")

_KIMI_ASSIST_TEMPLATE = _load_prompt("kimi_assist.md")
BERSERKER_KIMI_ASSIST_SYSTEM_PROMPT = _KIMI_ASSIST_TEMPLATE.replace(
    "{EVALUATION_RUBRICS}", EVALUATION_RUBRICS
)

RESPONSE_GENERATION_SYSTEM_PROMPT = _load_prompt("response_generation.md")
RUBRIC_GENERATION_SYSTEM_PROMPT = _load_prompt("rubric_generation.md")


# ---------------------------------------------------------------------------
# Bedrock Converse HTTP/2 (low-level)
# ---------------------------------------------------------------------------
def _bedrock_converse_http2(
    region_name, model_id, api_key, messages, system=None, inference_config=None
):
    encoded_model = urllib.parse.quote(model_id, safe="")
    url = f"https://bedrock-runtime.{region_name}.amazonaws.com/model/{encoded_model}/converse"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = {"messages": messages}
    if system:
        body["system"] = system
    if inference_config:
        body["inferenceConfig"] = inference_config

    resp = _BEDROCK_CLIENT.post(url, headers=headers, json=body)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# call_kimi_sync — single Bedrock Converse call with retries
# ---------------------------------------------------------------------------
def call_kimi_sync(api_key, system_prompt, user_content, temperature=0.2):
    bearer_token = api_key or os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
    if not bearer_token:
        raise RuntimeError(
            "Bedrock API key not found. Pass api_key or set AWS_BEARER_TOKEN_BEDROCK."
        )

    model_arn = _get_kimi_arn()
    region = _get_kimi_region()

    messages_param = [{"role": "user", "content": [{"text": user_content}]}]
    system_param = [{"text": system_prompt}] if system_prompt else None
    inference_config: dict = {"maxTokens": 65536}
    if temperature is not None:
        inference_config["temperature"] = float(temperature)

    last_exc = None
    _t0 = time.time()

    with _RATE_LIMITS["kimi"]:
        for attempt in range(1, _DEFAULT_MAX_RETRIES + 1):
            try:
                response = _bedrock_converse_http2(
                    region_name=region,
                    model_id=model_arn,
                    api_key=bearer_token,
                    messages=messages_param,
                    system=system_param,
                    inference_config=inference_config,
                )
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                if (
                    status_code in (429, 500, 502, 503)
                    and attempt < _DEFAULT_MAX_RETRIES
                ):
                    _logger.warning(
                        "Bedrock HTTP %d on attempt %d/%d — retrying",
                        status_code,
                        attempt,
                        _DEFAULT_MAX_RETRIES,
                    )
                    last_exc = e
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "kimi",
                    model_arn,
                    {},
                    success=False,
                    error_message=str(e),
                    duration=time.time() - _t0,
                )
                raise
            except Exception as e:
                _logger.warning(
                    "Bedrock request failed on attempt %d/%d: %s",
                    attempt,
                    _DEFAULT_MAX_RETRIES,
                    e,
                )
                last_exc = e
                if attempt < _DEFAULT_MAX_RETRIES:
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "kimi",
                    model_arn,
                    {},
                    success=False,
                    error_message=str(e),
                    duration=time.time() - _t0,
                )
                raise

            # Extract text from Converse response
            output_message = response.get("output", {}).get("message", {})
            content_blocks = output_message.get("content", [])
            text = ""
            for block in content_blocks:
                if "text" in block:
                    text = block["text"]
                    break
            text = text.strip()

            stop_reason = response.get("stopReason", "")

            # Content-filtered / guardrail-blocked → retry
            if not text and stop_reason in ("content_filtered", "guardrail_intervened"):
                _logger.warning(
                    "Bedrock response blocked (stopReason=%s) on attempt %d/%d",
                    stop_reason,
                    attempt,
                    _DEFAULT_MAX_RETRIES,
                )
                last_exc = RuntimeError(
                    f"Bedrock response blocked: stopReason={stop_reason}"
                )
                if attempt < _DEFAULT_MAX_RETRIES:
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "kimi",
                    model_arn,
                    {},
                    success=False,
                    error_message=str(last_exc),
                    duration=time.time() - _t0,
                )
                raise last_exc

            # Strip <think>...</think> reasoning blocks
            if "</think>" in text:
                text = text.rsplit("</think>", 1)[-1].strip()

            # Empty response after stripping → retry
            if not text:
                _logger.warning(
                    "Bedrock empty response after <think> stripping on attempt %d/%d",
                    attempt,
                    _DEFAULT_MAX_RETRIES,
                )
                last_exc = RuntimeError(
                    "Bedrock returned empty response after <think> stripping"
                )
                if attempt < _DEFAULT_MAX_RETRIES:
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "kimi",
                    model_arn,
                    {},
                    success=False,
                    error_message=str(last_exc),
                    duration=time.time() - _t0,
                )
                raise last_exc

            # Success
            usage_raw = response.get("usage", {})
            usage = {
                "input_tokens": int(usage_raw.get("inputTokens", 0)),
                "output_tokens": int(usage_raw.get("outputTokens", 0)),
            }
            _log_usage("kimi", model_arn, usage, duration=time.time() - _t0)
            return {"text": text, "usage": usage}

        _log_usage(
            "kimi",
            model_arn,
            {},
            success=False,
            error_message=str(last_exc),
            duration=time.time() - _t0,
        )
        raise last_exc or RuntimeError("Bedrock: all retry attempts exhausted")


# ---------------------------------------------------------------------------
# call_claude_sync — Claude 4.7 via AWS Bedrock Converse API
# ---------------------------------------------------------------------------


def call_claude_sync(api_key, system_prompt, user_content, temperature=None):
    bearer_token = api_key or os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
    if not bearer_token:
        raise RuntimeError(
            "Bedrock API key not found. Pass api_key or set AWS_BEARER_TOKEN_BEDROCK."
        )

    model_arn = _get_claude_arn()
    region = _get_claude_region()

    messages_param = [{"role": "user", "content": [{"text": user_content}]}]
    system_param = [{"text": system_prompt}] if system_prompt else None
    inference_config: dict = {"maxTokens": 65536}

    last_exc = None
    _t0 = time.time()
    with _RATE_LIMITS["claude"]:
        for attempt in range(1, _DEFAULT_MAX_RETRIES + 1):
            try:
                response = _bedrock_converse_http2(
                    region_name=region,
                    model_id=model_arn,
                    api_key=bearer_token,
                    messages=messages_param,
                    system=system_param,
                    inference_config=inference_config,
                )
            except httpx.HTTPStatusError as e:
                if (
                    e.response.status_code in (429, 500, 502, 503)
                    and attempt < _DEFAULT_MAX_RETRIES
                ):
                    _logger.warning(
                        "Claude Bedrock HTTP %d on attempt %d/%d",
                        e.response.status_code,
                        attempt,
                        _DEFAULT_MAX_RETRIES,
                    )
                    last_exc = e
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "claude",
                    model_arn,
                    {},
                    success=False,
                    error_message=str(e),
                    duration=time.time() - _t0,
                )
                raise
            except Exception as e:
                last_exc = e
                if attempt < _DEFAULT_MAX_RETRIES:
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "claude",
                    model_arn,
                    {},
                    success=False,
                    error_message=str(e),
                    duration=time.time() - _t0,
                )
                raise

            output_message = response.get("output", {}).get("message", {})
            text = ""
            for block in output_message.get("content", []):
                if "text" in block:
                    text = block["text"]
                    break
            text = text.strip()

            if "</think>" in text:
                text = text.rsplit("</think>", 1)[-1].strip()

            if not text:
                last_exc = RuntimeError("Claude Bedrock returned empty response")
                if attempt < _DEFAULT_MAX_RETRIES:
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "claude",
                    model_arn,
                    {},
                    success=False,
                    error_message=str(last_exc),
                    duration=time.time() - _t0,
                )
                raise last_exc

            usage_raw = response.get("usage", {})
            usage = {
                "input_tokens": int(usage_raw.get("inputTokens", 0)),
                "output_tokens": int(usage_raw.get("outputTokens", 0)),
            }
            _log_usage("claude", model_arn, usage, duration=time.time() - _t0)
            return {
                "text": text,
                "usage": usage,
            }

        _log_usage(
            "claude",
            model_arn,
            {},
            success=False,
            error_message=str(last_exc),
            duration=time.time() - _t0,
        )
        raise last_exc or RuntimeError("Claude Bedrock: all retry attempts exhausted")


# ---------------------------------------------------------------------------
# call_gemini_sync — Gemini 3.1 via Google AI Studio REST API
# ---------------------------------------------------------------------------


def _get_google_api_key():
    return (
        os.environ.get("GOOGLE_AI_STUDIO_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()


def call_gemini_sync(api_key, system_prompt, user_content, temperature=0.2, model=None):
    google_key = api_key or _get_google_api_key()
    if not google_key:
        raise RuntimeError(
            "Google API key not found. Pass api_key or set GOOGLE_AI_STUDIO_API_KEY / GOOGLE_API_KEY."
        )

    model_name = model or _get_gemini_model()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:streamGenerateContent"
        f"?key={google_key}&alt=sse"
    )

    gen_config: dict = {"maxOutputTokens": 65536}
    if temperature is not None:
        gen_config["temperature"] = float(temperature)

    body = {
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": gen_config,
    }
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    last_exc = None
    _t0 = time.time()
    with _RATE_LIMITS["gemini"]:
        for attempt in range(1, _DEFAULT_MAX_RETRIES + 1):
            try:
                with _HTTP_CLIENT.stream(
                    "POST", url, json=body, timeout=httpx.Timeout(600, connect=10.0)
                ) as resp:
                    if resp.status_code in (429, 500, 502, 503):
                        _resp_body = ""
                        try:
                            resp.read()
                            _resp_body = resp.text[:500]
                        except Exception:
                            pass
                        _logger.warning(
                            "Gemini HTTP %d on attempt %d/%d — %s",
                            resp.status_code,
                            attempt,
                            _DEFAULT_MAX_RETRIES,
                            _resp_body or "(unreadable)",
                        )
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                        if attempt < _DEFAULT_MAX_RETRIES:
                            time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                            continue
                        resp.raise_for_status()
                    resp.raise_for_status()

                    text_parts = []
                    usage_meta = {}
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        candidates = chunk.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            for part in parts:
                                if "text" in part and not part.get("thought"):
                                    text_parts.append(part["text"])
                        um = chunk.get("usageMetadata")
                        if um:
                            usage_meta = um

            except httpx.HTTPStatusError as e:
                if (
                    e.response.status_code in (429, 500, 502, 503)
                    and attempt < _DEFAULT_MAX_RETRIES
                ):
                    last_exc = e
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "gemini",
                    model_name,
                    {},
                    success=False,
                    error_message=str(e),
                    duration=time.time() - _t0,
                )
                raise
            except Exception as e:
                last_exc = e
                if attempt < _DEFAULT_MAX_RETRIES:
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "gemini",
                    model_name,
                    {},
                    success=False,
                    error_message=str(e),
                    duration=time.time() - _t0,
                )
                raise

            text = "".join(text_parts).strip()

            if not text:
                last_exc = RuntimeError("Gemini returned empty text")
                if attempt < _DEFAULT_MAX_RETRIES:
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "gemini",
                    model_name,
                    {},
                    success=False,
                    error_message=str(last_exc),
                    duration=time.time() - _t0,
                )
                raise last_exc

            usage = {
                "input_tokens": int(usage_meta.get("promptTokenCount", 0)),
                "output_tokens": int(usage_meta.get("candidatesTokenCount", 0)),
            }
            _log_usage("gemini", model_name, usage, duration=time.time() - _t0)
            return {
                "text": text,
                "usage": usage,
            }

        _log_usage(
            "gemini",
            model_name,
            {},
            success=False,
            error_message=str(last_exc),
            duration=time.time() - _t0,
        )
        raise last_exc or RuntimeError("Gemini: all retry attempts exhausted")


# ---------------------------------------------------------------------------
# call_openai_sync — GPT 5.4 via OpenAI Chat Completions API (streaming)
# ---------------------------------------------------------------------------


def _get_openai_api_key():
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def call_openai_sync(api_key, system_prompt, user_content, temperature=0.2, model=None):
    oai_key = api_key or _get_openai_api_key()
    if not oai_key:
        raise RuntimeError(
            "OpenAI API key not found. Pass api_key or set OPENAI_API_KEY."
        )

    model_name = model or _get_openai_model()
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {oai_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": model_name,
        "messages": [
            {"role": "developer", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_completion_tokens": 128000,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if temperature is not None:
        body["temperature"] = float(temperature)

    last_exc = None
    _t0 = time.time()
    with _RATE_LIMITS["openai"]:
        for attempt in range(1, _DEFAULT_MAX_RETRIES + 1):
            try:
                with _HTTP_CLIENT.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=body,
                    timeout=httpx.Timeout(600, connect=10.0),
                ) as resp:
                    if resp.status_code in (429, 500, 502, 503, 524):
                        _resp_body = ""
                        try:
                            resp.read()
                            _resp_body = resp.text[:500]
                        except Exception:
                            pass
                        _logger.warning(
                            "OpenAI HTTP %d on attempt %d/%d — %s",
                            resp.status_code,
                            attempt,
                            _DEFAULT_MAX_RETRIES,
                            _resp_body or "(unreadable)",
                        )
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                        if attempt < _DEFAULT_MAX_RETRIES:
                            time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                            continue
                        resp.raise_for_status()
                    resp.raise_for_status()

                    try:
                        text, usage_raw = _stream_openai_chat_completions(resp)
                    except (
                        RuntimeError,
                        httpx.StreamError,
                        httpx.ConnectError,
                        ConnectionResetError,
                        OSError,
                    ) as exc:
                        _logger.warning(
                            "OpenAI stream error on attempt %d/%d: %s",
                            attempt,
                            _DEFAULT_MAX_RETRIES,
                            exc,
                        )
                        last_exc = exc
                        if attempt < _DEFAULT_MAX_RETRIES:
                            time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                            continue
                        _log_usage(
                            "openai",
                            model_name,
                            {},
                            success=False,
                            error_message=str(exc),
                            duration=time.time() - _t0,
                        )
                        raise

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                _logger.warning(
                    "OpenAI request failed on attempt %d/%d: %s",
                    attempt,
                    _DEFAULT_MAX_RETRIES,
                    exc,
                )
                last_exc = exc
                if attempt < _DEFAULT_MAX_RETRIES:
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "openai",
                    model_name,
                    {},
                    success=False,
                    error_message=str(exc),
                    duration=time.time() - _t0,
                )
                raise

            if not text:
                last_exc = RuntimeError("OpenAI returned empty response")
                if attempt < _DEFAULT_MAX_RETRIES:
                    time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
                    continue
                _log_usage(
                    "openai",
                    model_name,
                    {},
                    success=False,
                    error_message=str(last_exc),
                    duration=time.time() - _t0,
                )
                raise last_exc

            cached = 0
            prompt_details = usage_raw.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict):
                cached = prompt_details.get("cached_tokens", 0)

            usage = {
                "input_tokens": int(usage_raw.get("prompt_tokens", 0)),
                "output_tokens": int(usage_raw.get("completion_tokens", 0)),
                "cached_tokens": int(cached),
            }
            _log_usage("openai", model_name, usage, duration=time.time() - _t0)
            return {
                "text": text,
                "usage": usage,
            }

        _log_usage(
            "openai",
            model_name,
            {},
            success=False,
            error_message=str(last_exc),
            duration=time.time() - _t0,
        )
        raise last_exc or RuntimeError("OpenAI: all retry attempts exhausted")


def _stream_openai_chat_completions(resp):
    accumulated_text = []
    usage = {}

    for raw_line in resp.iter_lines():
        if not raw_line or not raw_line.startswith("data: "):
            continue
        payload = raw_line[6:]
        if payload.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        choices = chunk.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            content = delta.get("content")
            if content:
                accumulated_text.append(content)

        chunk_usage = chunk.get("usage")
        if chunk_usage:
            usage = chunk_usage

    text = "".join(accumulated_text).strip()
    return text, usage


# ---------------------------------------------------------------------------
# JSON parsing helper
# ---------------------------------------------------------------------------
def _parse_json_from_response(text):
    """Extract JSON from LLM response text, stripping markdown fences if present."""
    if not text:
        return {}
    cleaned = text.strip()
    # Strip markdown code fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n", 1)
        if len(lines) > 1:
            cleaned = lines[1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}


# ===========================================================================
# KIMI ASSIST — unified 3-response eval + justification (single LLM call)
# ===========================================================================


def kimi_assist_eval(
    api_key, prompt, gpt_response, gemini_response, claude_response, rubric_names=None
):
    """
    Evaluate all 3 responses + generate justification in a single LLM call.

    Returns dict with keys: gpt, gemini, claude (each with 6 dimension scores
    + rubrics dict), and justification (comparative text).
    """
    user_content = (
        f"USER PROMPT:\n{prompt}\n\n"
        f"GPT RESPONSE:\n{gpt_response}\n\n"
        f"GEMINI RESPONSE:\n{gemini_response}\n\n"
        f"CLAUDE RESPONSE:\n{claude_response}"
    )
    if rubric_names:
        rubric_section = "\n".join(f"- {r}" for r in rubric_names)
        user_content += f"\n\nRUBRICS:\n{rubric_section}"

    result = call_kimi_sync(
        api_key=api_key,
        system_prompt=BERSERKER_KIMI_ASSIST_SYSTEM_PROMPT,
        user_content=user_content,
        temperature=0.2,
    )
    return _parse_json_from_response(result["text"])


# ===========================================================================
# RESPONSE GENERATION — parallel 3-model response generation from prompt
# ===========================================================================


def _call_model_with_retries(
    call_fn, call_kwargs, model_name, max_retries=3, usage_bid=False, usage_ctype=""
):
    if usage_bid or usage_ctype:
        set_usage_context(berserker_id=usage_bid, call_type=usage_ctype)

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            result = call_fn(**call_kwargs)
            text = (result.get("text") or "").strip()
            if text:
                return text
            last_exc = RuntimeError(f"{model_name} returned empty response")
        except Exception as e:
            last_exc = e
        if attempt < max_retries:
            _logger.warning(
                "%s attempt %d/%d failed — retrying",
                model_name,
                attempt,
                max_retries,
            )
            time.sleep(min(2**attempt, _DEFAULT_BACKOFF_MAX))
    return f"[Error: {model_name} response generation failed: {last_exc}]"


def generate_responses_from_prompt(prompt):
    """
    Generate responses from GPT, Gemini, and Claude in parallel.

    Returns dict: {"gpt_response": str, "gemini_response": str, "claude_response": str}
    """
    parent_bid = getattr(_usage_context, "berserker_id", False)
    parent_ctype = getattr(_usage_context, "call_type", "response_gen")

    model_specs = {
        "gpt_response": (
            call_openai_sync,
            {
                "api_key": _get_openai_api_key(),
                "system_prompt": RESPONSE_GENERATION_SYSTEM_PROMPT,
                "user_content": prompt,
            },
            "GPT",
        ),
        "gemini_response": (
            call_gemini_sync,
            {
                "api_key": _get_google_api_key(),
                "system_prompt": RESPONSE_GENERATION_SYSTEM_PROMPT,
                "user_content": prompt,
            },
            "Gemini",
        ),
        "claude_response": (
            call_claude_sync,
            {
                "api_key": "",
                "system_prompt": RESPONSE_GENERATION_SYSTEM_PROMPT,
                "user_content": prompt,
            },
            "Claude",
        ),
    }

    results = {}
    futures = {
        _RESPONSE_GEN_POOL.submit(
            _call_model_with_retries,
            call_fn,
            call_kwargs,
            model_name,
            3,
            parent_bid,
            parent_ctype,
        ): response_key
        for response_key, (call_fn, call_kwargs, model_name) in model_specs.items()
    }
    for future in as_completed(futures):
        response_key = futures[future]
        try:
            results[response_key] = future.result()
        except Exception as e:
            model_name = model_specs[response_key][2]
            results[response_key] = (
                f"[Error: {model_name} response generation failed: {e}]"
            )

    return results


# ===========================================================================
# CRITERIA GENERATION — generate evaluation criteria from prompt
# ===========================================================================

_DEFAULT_CRITERIA = [
    "Accuracy of technical content",
    "Completeness of response",
    "Practical applicability",
]


def generate_criteria_from_prompt(prompt):
    """
    Generate evaluation rubrics from a user prompt using Kimi.

    Returns list of strings: ["rubric 1", "rubric 2", ...]
    """
    result = call_kimi_sync(
        api_key="",
        system_prompt=RUBRIC_GENERATION_SYSTEM_PROMPT,
        user_content=prompt,
    )
    parsed = _parse_json_from_response(result["text"])

    if isinstance(parsed, list) and parsed:
        return [str(c) for c in parsed if c]
    if isinstance(parsed, dict):
        for key in ("rubrics", "criteria", "evaluation_criteria", "dimensions"):
            val = parsed.get(key)
            if isinstance(val, list) and val:
                return [str(c) for c in val if c]

    return list(_DEFAULT_CRITERIA)


# ===========================================================================
# EVALUATION — pointwise single-response rating
# ===========================================================================


def eval_single_response_kimi(api_key, prompt, response_text):
    """
    Evaluate a single model response on 6 dimensions using Bedrock.

    Returns dict with scores and reasons, e.g.:
        {"instruction_following": {"score": 5, "reason": "..."}, ...}
    """
    user_content = f"USER PROMPT:\n{prompt}\n\nRESPONSE:\n{response_text}"
    result = call_kimi_sync(
        api_key=api_key,
        system_prompt=BERSERKER_EVAL_SYSTEM_PROMPT,
        user_content=user_content,
        temperature=0.2,
    )
    parsed = _parse_json_from_response(result["text"])
    # The response is nested under "response" key
    return parsed.get("response", parsed)


def eval_all_responses_kimi(
    api_key, prompt, gpt_response, gemini_response, claude_response
):
    """
    Run 3 parallel evaluations — one per model response (GPT, Gemini, Claude).

    Returns dict:
        {
          "gpt": {"instruction_following": {"score": 5, "reason": "..."}, ...},
          "gemini": {...},
          "claude": {...},
        }
    """
    results = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                eval_single_response_kimi, api_key, prompt, gpt_response
            ): "gpt",
            executor.submit(
                eval_single_response_kimi, api_key, prompt, gemini_response
            ): "gemini",
            executor.submit(
                eval_single_response_kimi, api_key, prompt, claude_response
            ): "claude",
        }
        for future in as_completed(futures):
            model_key = futures[future]
            try:
                results[model_key] = future.result()
            except Exception as e:
                _logger.error("Eval failed for %s: %s", model_key, e)
                results[model_key] = {"error": str(e)}

    return results


# ===========================================================================
# Score extraction helpers
# ===========================================================================

# Maps API dimension names → Odoo field suffixes
_EVAL_DIM_MAP = {
    "instruction_following": "instruction_following",
    "truthfulness": "truthfulness",
    "prompt_correctness": "prompt_correctness",
    "writing_style": "writing_quality",
    "verbosity": "verbosity",
    "overall_quality": "overall_quality",
}


def _extract_eval_scores(eval_result):
    """
    Extract scores from an eval_single_response_kimi result.

    Maps API dimension names to field names:
        "writing_style" -> "writing_quality"
    Returns dict of {field_name: {"score": str_score, "reason": str_reason}}.
    """
    out = {}
    if not eval_result or "error" in eval_result:
        return out
    for api_dim, field_name in _EVAL_DIM_MAP.items():
        dim_data = eval_result.get(api_dim, {})
        score = dim_data.get("score", "")
        reason = dim_data.get("reason", "")
        out[field_name] = {
            "score": str(score) if score != "" else "",
            "reason": str(reason),
        }
    return out


def _to_selection_score(raw, min_val, max_val):
    """
    Convert raw score to string for Odoo Selection field.
    Returns "" if invalid or out of range.
    """
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return ""
    if min_val <= val <= max_val:
        return str(val)
    return ""


# ===========================================================================
# QC — quality checks on human evaluations
# ===========================================================================


def perform_qc_checks_kimi(
    api_key, prompt, gpt_response, gemini_response, claude_response, justification
):
    """
    Run QC checks on a human evaluation justification.

    Returns parsed QC result dict:
        {"qc_status": "pass"/"fail", "checks": [...]}
    """
    qc_input = {
        "prompt": prompt,
        "gpt_response": gpt_response,
        "gemini_response": gemini_response,
        "claude_response": claude_response,
        "justification": justification,
    }
    user_content = f"EVALUATION TO QC:\n{json.dumps(qc_input, indent=2)}"

    result = call_kimi_sync(
        api_key=api_key,
        system_prompt=BERSERKER_QC_SYSTEM_PROMPT,
        user_content=user_content,
        temperature=0.2,
    )
    parsed = _parse_json_from_response(result["text"])

    # Normalise qc_status
    status = parsed.get("qc_status", "fail").lower()
    checks = parsed.get("checks", [])
    if any(c.get("result", "").lower() == "fail" for c in checks):
        status = "fail"

    return {
        "qc_status": status,
        "checks": checks,
    }
