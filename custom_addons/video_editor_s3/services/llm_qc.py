# -*- coding: utf-8 -*-
import json
import logging
import os
import random
import re
import time

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"
DEFAULT_REGION = "ap-south-1"
DEFAULT_MAX_TOKENS = 1500
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_ATTEMPTS = 3

_RETRYABLE_ERROR_NAMES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelTimeoutException",
    "ModelStreamErrorException",
    "RequestTimeout",
}

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


def _is_retryable(exc: BaseException) -> bool:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code", "")
        if code in _RETRYABLE_ERROR_NAMES:
            return True
    name = exc.__class__.__name__
    if name in _RETRYABLE_ERROR_NAMES:
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _build_user_turn(prompt: str) -> str:
    return (
        "PROMPT_TO_EVALUATE:\n"
        "------\n"
        f"{prompt}\n"
        "------\n\n"
        "Return ONLY a fenced ```json block with keys: score (integer 0-100), "
        "expert_level (one of 'novice', 'intermediate', 'advanced', 'expert'), "
        "quality ('pass' or 'fail'), reason (string), issues (array of strings)."
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
            "LLM QC response did not contain a parseable JSON block. "
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
    return {
        "score": score,
        "expert_level": expert_level,
        "quality": quality,
        "reason": reason,
        "issues": issues,
        "raw_json": json.dumps(candidate, ensure_ascii=False),
    }


def evaluate_prompt(
    *,
    prompt: str,
    seed_prompt: str,
    access_key: str,
    secret_key: str,
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
            "QC seed prompt is empty. Configure it in Settings > Video Editor S3."
        ))
    if not access_key or not secret_key:
        raise UserError(_(
            "Bedrock credentials missing. Configure access key and secret key in "
            "Settings > Video Editor S3."
        ))
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise UserError(_(
            "boto3 is required for LLM QC. Install with: pip install boto3"
        )) from exc

    boto_config = Config(
        retries={"max_attempts": 0, "mode": "standard"},
        read_timeout=180,
        connect_timeout=10,
    )
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=boto_config,
    )

    user_turn = _build_user_turn(prompt)
    last_exc = None
    resp = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.converse(
                modelId=model_id,
                system=[{"text": seed_prompt}],
                messages=[{"role": "user", "content": [{"text": user_turn}]}],
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                },
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable(exc):
                raise UserError(_(
                    "Bedrock call failed: %s: %s"
                ) % (exc.__class__.__name__, exc)) from exc
            delay = min(30.0, (2 ** attempt) + random.random())
            _logger.warning(
                "Bedrock QC retry %d/%d in %.1fs (%s)",
                attempt, max_attempts, delay, exc.__class__.__name__,
            )
            time.sleep(delay)
    if resp is None:
        raise UserError(_(
            "Bedrock QC call exhausted after %d attempts: %s"
        ) % (max_attempts, last_exc))

    try:
        blocks = resp["output"]["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise UserError(_(
            "Unexpected Bedrock response shape: %s"
        ) % (str(resp)[:300],)) from exc
    text_parts = [b.get("text", "") for b in blocks if "text" in b]
    text = "".join(text_parts).strip()
    if not text:
        raise UserError(_("Bedrock QC returned empty text."))

    parsed = _parse_qc_response(text)
    usage = resp.get("usage") or {}
    parsed["input_tokens"] = int(usage.get("inputTokens") or 0)
    parsed["output_tokens"] = int(usage.get("outputTokens") or 0)
    return parsed
