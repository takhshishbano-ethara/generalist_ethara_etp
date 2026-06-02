# -*- coding: utf-8 -*-
"""T2AV multimodal QC client (OpenRouter, requests-only).

Sends one dataset row (metadata + prompt + video clip) to a multimodal
vision-language model through OpenRouter's OpenAI-compatible REST API
and returns the parsed verdict. No litellm dependency.
"""
import base64
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import subprocess
import tempfile
import time

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "openrouter/google/gemini-3.1-pro-preview"
DEFAULT_MAX_TOKENS = 32000
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TOP_P = 0.9
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_REASONING_EFFORT = "high"

_CONNECT_TIMEOUT = 15
_READ_TIMEOUT = 600
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504, 509}

_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_SEED_PROMPT_PATH = os.path.join(_MODULE_ROOT, "data", "llm_qc_seed.md")

_JSON_BLOCK_RE = re.compile(r"```json\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)
_TRAILING_JSON_RE = re.compile(r"(\{.*\})\s*$", re.DOTALL)
_VALID_VERDICTS = {"PASS", "FAIL", "FLAG"}


def load_default_seed_prompt() -> str:
    try:
        with open(_DEFAULT_SEED_PROMPT_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        _logger.warning("Could not load default LLM QC seed prompt: %s", exc)
        return ""


def _normalize_verdict(value, default="FAIL"):
    if not value:
        return default
    v = str(value).strip().upper()
    return v if v in _VALID_VERDICTS else default


def _coerce_str(value):
    if value is None:
        return ""
    return str(value).strip()


def probe_video(video_bytes: bytes) -> dict:
    ffprobe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    if not ffprobe or not os.path.exists(ffprobe):
        return {
            "actual_resolution": "",
            "actual_fps": 0.0,
            "actual_duration_s": 0.0,
            "audio_stream": False,
        }
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
        fh.write(video_bytes)
        tmp_path = fh.name
    try:
        cmd = [
            ffprobe, "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", tmp_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return {
                "actual_resolution": "",
                "actual_fps": 0.0,
                "actual_duration_s": 0.0,
                "audio_stream": False,
            }
        info = json.loads(proc.stdout or "{}")
        streams = info.get("streams") or []
        v = next((s for s in streams if s.get("codec_type") == "video"), {})
        a = next((s for s in streams if s.get("codec_type") == "audio"), {})
        width = int(v.get("width") or 0)
        height = int(v.get("height") or 0)
        resolution = "%dx%d" % (width, height) if width and height else ""
        fps_str = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/0"
        try:
            num, den = fps_str.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        duration = float((info.get("format") or {}).get("duration") or 0.0)
        return {
            "actual_resolution": resolution,
            "actual_fps": round(fps, 3),
            "actual_duration_s": round(duration, 3),
            "audio_stream": bool(a),
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        _logger.warning("ffprobe failed: %s", exc)
        return {
            "actual_resolution": "",
            "actual_fps": 0.0,
            "actual_duration_s": 0.0,
            "audio_stream": False,
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _build_user_turn(
    *,
    item_id: str,
    category: str,
    sub_category: str,
    description: str,
    topic: str,
    prompt: str,
    resolution: str,
    duration_seconds,
    style: str,
    fps,
    complexity: str,
    language: str,
    probe: dict,
) -> str:
    lines = [
        "ITEM_ID: %s" % _coerce_str(item_id),
        "CATEGORY: %s" % _coerce_str(category),
        "SUBCATEGORY: %s" % _coerce_str(sub_category),
        "DESCRIPTION: %s" % _coerce_str(description),
        "TOPIC: %s" % _coerce_str(topic),
        "PROMPT: %s" % _coerce_str(prompt),
        "RES: %s" % _coerce_str(resolution),
        "DURATION: %s" % _coerce_str(duration_seconds),
        "STYLE: %s" % _coerce_str(style),
        "FPS: %s" % _coerce_str(fps),
        "COMPLEXITY: %s" % _coerce_str(complexity),
        "LANGUAGE: %s" % _coerce_str(language or "English"),
        "",
        "ACTUAL_RESOLUTION: %s" % _coerce_str(probe.get("actual_resolution")),
        "ACTUAL_FPS: %s" % _coerce_str(probe.get("actual_fps")),
        "ACTUAL_DURATION_S: %s" % _coerce_str(probe.get("actual_duration_s")),
        "AUDIO_STREAM: %s" % ("True" if probe.get("audio_stream") else "False"),
        "",
        "VIDEO: attached below.",
    ]
    return "\n".join(lines)


def _encode_video_data_url(video_bytes: bytes, filename: str) -> str:
    mime, _ext = mimetypes.guess_type(filename or "video.mp4")
    if not mime or not mime.startswith("video/"):
        mime = "video/mp4"
    b64 = base64.b64encode(video_bytes).decode("ascii")
    return "data:%s;base64,%s" % (mime, b64)


def _parse_qc_response(text: str) -> dict:
    candidate = None
    last_err = None
    matches = list(_JSON_BLOCK_RE.finditer(text or ""))
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
        m = _TRAILING_JSON_RE.search(text or "")
        if m:
            try:
                obj = json.loads(m.group(1).strip())
                if isinstance(obj, dict):
                    candidate = obj
            except json.JSONDecodeError as e:
                last_err = e
    if candidate is None:
        raise UserError(_(
            "LLM QC response did not contain a parseable JSON block. "
            "Last decode error: %s. Excerpt: %s"
        ) % (last_err, (text or "")[:400]))
    return {
        "qc_result": _normalize_verdict(candidate.get("qc_result")),
        "failure_reason": _coerce_str(candidate.get("failure_reason")),
        "fixed_prompt": _coerce_str(candidate.get("fixed_prompt")),
    }


class _RetryableHTTP(Exception):
    def __init__(self, status, body):
        super().__init__("HTTP %d: %s" % (status, body[:200]))
        self.status = status
        self.body = body


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _RetryableHTTP):
        return True
    return isinstance(exc, (
        requests.ConnectionError, requests.Timeout,
        requests.exceptions.ChunkedEncodingError,
        ConnectionError, TimeoutError, OSError,
    ))


def _strip_provider_prefix(model_id: str) -> str:
    return model_id.split("openrouter/", 1)[-1] if model_id.startswith("openrouter/") else model_id


def _call_openrouter(*, system_prompt, user_text, video_data_url, model_id,
                    api_key, max_tokens, temperature):
    payload = {
        "model": _strip_provider_prefix(model_id),
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": video_data_url}},
                ],
            },
        ],
        "temperature": temperature,
        "top_p": DEFAULT_TOP_P,
        "max_tokens": max_tokens,
        "reasoning": {"effort": DEFAULT_REASONING_EFFORT},
    }
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://crowley-sourcing.local",
        "X-Title": "Crowley Sourcing LLM QC",
    }
    resp = requests.post(
        _OPENROUTER_URL,
        json=payload,
        headers=headers,
        timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
    )
    if resp.status_code in _RETRYABLE_HTTP_STATUSES:
        raise _RetryableHTTP(resp.status_code, resp.text)
    if resp.status_code >= 400:
        raise UserError(_(
            "OpenRouter call failed (HTTP %d): %s"
        ) % (resp.status_code, resp.text[:400]))
    try:
        data = resp.json()
    except ValueError as exc:
        raise UserError(_(
            "OpenRouter returned non-JSON response: %s"
        ) % resp.text[:400]) from exc
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
        text = (message.get("content") or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise UserError(_(
            "Unexpected OpenRouter response shape: %s"
        ) % (str(data)[:400],)) from exc
    return text


def evaluate_llm_qc(
    *,
    item_id: str,
    category: str,
    sub_category: str,
    description: str,
    topic: str,
    prompt: str,
    resolution: str,
    duration_seconds,
    style: str,
    fps,
    complexity: str = "",
    language: str = "English",
    video_bytes: bytes,
    video_filename: str,
    seed_prompt: str,
    openrouter_api_key: str,
    model_id: str = DEFAULT_MODEL_ID,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict:
    """Run one LLM QC pass via OpenRouter REST.

    Returns dict with qc_result, failure_reason, fixed_prompt, raw_text, probe.
    """
    if not seed_prompt or not seed_prompt.strip():
        raise UserError(_(
            "LLM QC seed prompt is empty. "
            "Configure it in Settings > Crowly Sourcing > LLM QC."
        ))
    if not openrouter_api_key or not openrouter_api_key.strip():
        raise UserError(_(
            "OpenRouter API key missing. "
            "Configure it in Settings > Crowly Sourcing > LLM QC."
        ))
    if not model_id or not model_id.strip():
        raise UserError(_("LLM QC model id is empty."))
    if not video_bytes:
        raise UserError(_("Video bytes are empty; cannot run LLM QC."))

    probe = probe_video(video_bytes)
    user_text = _build_user_turn(
        item_id=item_id, category=category, sub_category=sub_category,
        description=description, topic=topic, prompt=prompt,
        resolution=resolution, duration_seconds=duration_seconds, style=style,
        fps=fps, complexity=complexity, language=language, probe=probe,
    )
    video_data_url = _encode_video_data_url(video_bytes, video_filename or "video.mp4")

    last_exc = None
    text = None
    for attempt in range(1, max_attempts + 1):
        try:
            text = _call_openrouter(
                system_prompt=seed_prompt.strip(), user_text=user_text,
                video_data_url=video_data_url, model_id=model_id.strip(),
                api_key=openrouter_api_key.strip(), max_tokens=max_tokens,
                temperature=temperature,
            )
            if text:
                break
            last_exc = _RetryableHTTP(0, "empty response")
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable(exc):
                if isinstance(exc, UserError):
                    raise
                raise UserError(_(
                    "LLM QC call failed: %s: %s"
                ) % (exc.__class__.__name__, exc)) from exc
            delay = min(30.0, (2 ** attempt) + random.random())
            _logger.warning(
                "LLM QC retry %d/%d in %.1fs (%s)",
                attempt, max_attempts, delay, exc.__class__.__name__,
            )
            time.sleep(delay)

    if not text:
        raise UserError(_(
            "LLM QC call exhausted after %d attempts: %s"
        ) % (max_attempts, last_exc))

    parsed = _parse_qc_response(text)
    parsed["raw_text"] = text
    parsed["probe"] = probe
    return parsed
