# -*- coding: utf-8 -*-
"""Kimi K2.5 prompt-QC client.

Talks to a Moonshot Kimi K2.5 deployment exposed through AWS Bedrock
Marketplace via the Bedrock-runtime REST endpoint and a long-lived
Bedrock API key (bearer token). The model identifier is a SageMaker
Marketplace endpoint ARN which is URL-encoded into the path. This mirrors
the canonical pattern in ``task_forge_core/services/kimi_client.py``.

The public entry point ``evaluate_prompt`` is intentionally signature-
compatible with the previous boto3-based implementation so the rest of
the module (jobs, settings resolver) does not change.
"""
import json
import logging
import os
import random
import re
import time
from urllib.parse import quote

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = ""
DEFAULT_REGION = "us-east-1"
DEFAULT_MAX_TOKENS = 1500
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_ATTEMPTS = 3
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 180

BEDROCK_CONVERSE_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
)

_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504, 509}

_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_SEED_PROMPT_PATH = os.path.join(_MODULE_ROOT, "data", "qc_seed_prompt.md")

_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def load_default_seed_prompt() -> str:
    try:
        with open(_DEFAULT_SEED_PROMPT_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        _logger.warning("Could not load default QC seed prompt: %s", exc)
        return ""


class _RetryableHTTP(Exception):
    def __init__(self, status, body):
        super().__init__("HTTP %d: %s" % (status, body[:200]))
        self.status = status
        self.body = body


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _RetryableHTTP):
        return True
    return isinstance(exc, (requests.ConnectionError, requests.Timeout, ConnectionError, TimeoutError, OSError))


def _build_user_turn(prompt: str) -> str:
    return (
        "PROMPT_TO_EVALUATE:\n"
        "------\n"
        f"{prompt}\n"
        "------\n\n"
        "Return ONLY a fenced ```json block with keys: score (integer 0-100), "
        "expert_level (one of 'novice', 'intermediate', 'advanced', 'expert'), "
        "quality ('pass' or 'fail'), reason (string), issues (array of strings), "
        "corrected_prompt (string — a rewritten production-grade version of the "
        "prompt that fixes every issue; empty string if the prompt violates policy)."
    )


def _parse_qc_response(text: str) -> dict:
    matches = list(_JSON_BLOCK_RE.finditer(text or ""))
    candidate = None
    last_err = None
    for m in reversed(matches):
        try:
            obj = json.loads(m.group(1).strip())
        except json.JSONDecodeError as e:
            last_err = e
            continue
        if isinstance(obj, dict):
            candidate = obj
            break
    if candidate is None:
        raise UserError(_(
            "Kimi K2.5 QC response did not contain a parseable JSON block. "
            "Last decode error: %s. Excerpt: %s"
        ) % (last_err, (text or "")[:400]))

    score_raw = candidate.get("score")
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        score = 0.0
    quality_raw = (candidate.get("quality") or "").strip().lower()
    quality = quality_raw if quality_raw in ("pass", "fail") else "fail"
    expert_level = (candidate.get("expert_level") or "").strip()
    reason = (candidate.get("reason") or "").strip()
    issues_raw = candidate.get("issues") or []
    if isinstance(issues_raw, list):
        issues = "\n".join(str(x) for x in issues_raw if x)
    else:
        issues = str(issues_raw)
    corrected_raw = candidate.get("corrected_prompt")
    corrected_prompt = corrected_raw.strip() if isinstance(corrected_raw, str) else ""
    return {
        "score": score,
        "expert_level": expert_level,
        "quality": quality,
        "reason": reason,
        "issues": issues,
        "corrected_prompt": corrected_prompt,
        "raw_json": json.dumps(candidate, ensure_ascii=False),
    }


def _call_kimi(region: str, model_id: str, api_key: str, payload: dict) -> dict:
    url = BEDROCK_CONVERSE_URL.format(
        region=region,
        model_id=quote(model_id, safe=""),
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % api_key,
    }
    resp = requests.post(
        url,
        json=payload,
        headers=headers,
        timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
    )
    if resp.status_code in _RETRYABLE_HTTP_STATUSES:
        raise _RetryableHTTP(resp.status_code, resp.text)
    if resp.status_code >= 400:
        raise UserError(_(
            "Kimi K2.5 (Bedrock) call failed (HTTP %d): %s"
        ) % (resp.status_code, resp.text[:400]))
    try:
        return resp.json()
    except ValueError as exc:
        raise UserError(_(
            "Kimi K2.5 returned non-JSON response: %s"
        ) % resp.text[:400]) from exc


def evaluate_prompt(
    *,
    prompt: str,
    seed_prompt: str,
    api_key: str,
    region: str = DEFAULT_REGION,
    model_id: str = DEFAULT_MODEL_ID,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict:
    if not prompt or not prompt.strip():
        raise UserError(_("Prompt to evaluate is empty."))
    if not seed_prompt or not seed_prompt.strip():
        raise UserError(_(
            "QC seed prompt is empty. Configure it in Settings > Crowley Sourcing."
        ))
    if not api_key or not api_key.strip():
        raise UserError(_(
            "Bedrock API key missing. Configure it in Settings > Crowley Sourcing > Kimi K2.5 Prompt QC."
        ))
    if not model_id or not model_id.strip():
        raise UserError(_(
            "Kimi K2.5 model ARN missing. Configure it in Settings > Crowley Sourcing > Kimi K2.5 Prompt QC."
        ))

    user_turn = _build_user_turn(prompt)
    payload = {
        "messages": [
            {"role": "user", "content": [{"text": user_turn}]},
        ],
        "system": [{"text": seed_prompt}],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    }

    last_exc = None
    result = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = _call_kimi(region.strip(), model_id.strip(), api_key.strip(), payload)
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable(exc):
                if isinstance(exc, UserError):
                    raise
                raise UserError(_(
                    "Kimi K2.5 call failed: %s: %s"
                ) % (exc.__class__.__name__, exc)) from exc
            delay = min(30.0, (2 ** attempt) + random.random())
            _logger.warning(
                "Kimi K2.5 QC retry %d/%d in %.1fs (%s)",
                attempt, max_attempts, delay, exc.__class__.__name__,
            )
            time.sleep(delay)
    if result is None:
        raise UserError(_(
            "Kimi K2.5 QC call exhausted after %d attempts: %s"
        ) % (max_attempts, last_exc))

    try:
        blocks = result["output"]["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise UserError(_(
            "Unexpected Kimi K2.5 response shape: %s"
        ) % (str(result)[:300],)) from exc
    text_parts = [b.get("text", "") for b in blocks if "text" in b]
    text = "".join(text_parts).strip()
    if not text:
        raise UserError(_("Kimi K2.5 QC returned empty text."))

    parsed = _parse_qc_response(text)
    usage = result.get("usage") or {}
    parsed["input_tokens"] = int(usage.get("inputTokens") or 0)
    parsed["output_tokens"] = int(usage.get("outputTokens") or 0)
    return parsed
