# -*- coding: utf-8 -*-
"""T2AV multimodal QC client (OpenRouter + LiteLLM + Gemini).

Sends one dataset row (metadata + prompt + video clip) to a multimodal
vision-language model through OpenRouter and returns the parsed verdict
dict matching the schema defined in ``data/llm_qc_seed.md``: a single
fenced ```json block with exactly three keys (``qc_result``,
``failure_reason``, ``fixed_prompt``).

The clip is uploaded as a base64-encoded ``data:video/mp4;base64,...``
URL in a single ``video_url`` content-part on the user turn. The system
turn is the verbatim seed prompt. The user turn carries the declared
metadata plus ffprobe-derived ground-truth ``ACTUAL_*`` values so the
reviewer can cross-check declared vs actual.
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
import time

from odoo import _
from odoo.exceptions import UserError

os.environ.setdefault("LITELLM_LOG", "ERROR")
os.environ.setdefault("LITELLM_DROP_PARAMS", "True")

try:
    import litellm  # type: ignore
    _LITELLM_AVAILABLE = True
except ImportError:
    litellm = None  # type: ignore
    _LITELLM_AVAILABLE = False

_logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "openrouter/google/gemini-3.1-pro-preview"
DEFAULT_MAX_TOKENS = 64000
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TOP_P = 0.9
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_MAX_ATTEMPTS = 2
_READ_TIMEOUT = 600

_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_SEED_PROMPT_PATH = os.path.join(_MODULE_ROOT, "data", "llm_qc_seed.md")

_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
_VERDICTS = ("PASS", "FAIL", "FLAG")

if _LITELLM_AVAILABLE and litellm is not None:
    litellm.suppress_debug_info = True
    litellm.set_verbose = False
    litellm.json_logs = False
    for _name in ("LiteLLM", "litellm", "litellm.utils", "litellm.main",
                  "litellm.litellm_core_utils", "httpx", "httpcore", "openai"):
        logging.getLogger(_name).setLevel(logging.WARNING)
        logging.getLogger(_name).propagate = False


def load_default_seed_prompt() -> str:
    try:
        with open(_DEFAULT_SEED_PROMPT_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        _logger.warning("Could not load default LLM QC seed prompt: %s", exc)
        return ""


def _normalize_verdict(value, default="FAIL"):
    if not isinstance(value, str):
        return default
    v = value.strip().upper()
    return v if v in _VERDICTS else default


def _coerce_str(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


_FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"


def probe_video(video_bytes: bytes) -> dict:
    """Run ffprobe over piped bytes and return resolution/fps/duration/audio dict."""
    result = {
        "actual_resolution": "",
        "actual_fps": None,
        "actual_duration_s": None,
        "audio_stream": False,
    }
    if not video_bytes or not os.path.exists(_FFPROBE):
        return result
    try:
        proc = subprocess.run(
            [
                _FFPROBE, "-v", "error", "-print_format", "json",
                "-show_streams", "-show_format", "-i", "pipe:0",
            ],
            input=video_bytes, capture_output=True, timeout=60, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _logger.warning("ffprobe failed: %s", exc)
        return result
    if proc.returncode != 0:
        return result
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return result

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    if video:
        w, h = video.get("width"), video.get("height")
        if w and h:
            result["actual_resolution"] = "%dx%d" % (int(w), int(h))
        fps_raw = video.get("avg_frame_rate") or video.get("r_frame_rate") or ""
        if "/" in fps_raw:
            num, den = fps_raw.split("/", 1)
            try:
                n, d = float(num), float(den)
                if d > 0:
                    result["actual_fps"] = round(n / d, 3)
            except (TypeError, ValueError):
                pass
        elif fps_raw:
            try:
                result["actual_fps"] = round(float(fps_raw), 3)
            except (TypeError, ValueError):
                pass

    dur_raw = fmt.get("duration") or (video.get("duration") if video else None)
    if dur_raw:
        try:
            result["actual_duration_s"] = round(float(dur_raw), 3)
        except (TypeError, ValueError):
            pass

    result["audio_stream"] = bool(has_audio)
    return result


def _build_user_turn(
    *, item_id, category, sub_category, description, topic, prompt,
    resolution, duration_seconds, style, fps, complexity, language,
    probe,
):
    probe = probe or {}
    return "\n".join([
        f"ITEM_ID: {item_id or ''}",
        f"CATEGORY: {category or ''}",
        f"SUBCATEGORY: {sub_category or ''}",
        f"DESCRIPTION: {description or ''}",
        f"TOPIC: {topic or ''}",
        f"PROMPT: {prompt or ''}",
        f"RES: {resolution or ''}",
        f"DURATION: {duration_seconds if duration_seconds is not None else ''}",
        f"STYLE: {style or ''}",
        f"FPS: {fps if fps is not None else ''}",
        f"COMPLEXITY: {complexity or ''}",
        f"LANGUAGE: {language or ''}",
        f"ACTUAL_RESOLUTION: {probe.get('actual_resolution') or ''}",
        f"ACTUAL_FPS: {probe.get('actual_fps') if probe.get('actual_fps') is not None else ''}",
        f"ACTUAL_DURATION_S: {probe.get('actual_duration_s') if probe.get('actual_duration_s') is not None else ''}",
        f"HAS_AUDIO: {probe.get('audio_stream', False)}",
        "",
        "VIDEO: attached as a video_url content part below.",
        "Cross-check the declared RES/DURATION/FPS against ACTUAL_* values; flag mismatches.",
    ])


def _encode_video_data_url(video_bytes: bytes, filename: str) -> str:
    mime = None
    if filename:
        mime, _ = mimetypes.guess_type(filename)
    if not mime or not mime.startswith("video/"):
        mime = "video/mp4"
    b64 = base64.b64encode(video_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


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

    return {
        "qc_result": _normalize_verdict(candidate.get("qc_result")),
        "failure_reason": _coerce_str(candidate.get("failure_reason")),
        "fixed_prompt": _coerce_str(candidate.get("fixed_prompt")),
        "raw_json": json.dumps(candidate, ensure_ascii=False),
    }


def _is_retryable(exc: BaseException) -> bool:
    if not _LITELLM_AVAILABLE:
        return False
    name = exc.__class__.__name__
    if name in {"Timeout", "APIConnectionError", "ServiceUnavailableError",
                "InternalServerError", "RateLimitError"}:
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _call_openrouter(*, system_prompt, user_text, video_data_url, model_id,
                     api_key, max_tokens, temperature):
    if not _LITELLM_AVAILABLE or litellm is None:
        raise UserError(_(
            "litellm is not installed on the Odoo host. "
            "Install it with: pip install litellm"
        ))
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "video_url", "video_url": {"url": video_data_url}},
            ],
        },
    ]
    return litellm.completion(
        model=model_id, messages=messages, api_key=api_key,
        temperature=temperature, top_p=DEFAULT_TOP_P, max_tokens=max_tokens,
        timeout=_READ_TIMEOUT, num_retries=0, max_retries=0, drop_params=True,
        extra_body={"reasoning": {"effort": DEFAULT_REASONING_EFFORT}},
    )


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
    """Run one LLM QC pass over (metadata + prompt + video).

    Returns a dict with: qc_result, failure_reason, fixed_prompt, raw_text,
    plus the ffprobe-derived probe dict under 'probe'.
    """
    if not _LITELLM_AVAILABLE:
        raise UserError(_(
            "litellm is not installed on the Odoo host. "
            "Install it with: pip install litellm"
        ))
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
    response = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = _call_openrouter(
                system_prompt=seed_prompt.strip(), user_text=user_text,
                video_data_url=video_data_url, model_id=model_id.strip(),
                api_key=openrouter_api_key.strip(), max_tokens=max_tokens,
                temperature=temperature,
            )
            break
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

    if response is None:
        raise UserError(_(
            "LLM QC call exhausted after %d attempts: %s"
        ) % (max_attempts, last_exc))

    try:
        choice = response.choices[0]
    except (AttributeError, IndexError) as exc:
        raise UserError(_(
            "Unexpected LLM QC response shape: %s"
        ) % (str(response)[:300],)) from exc
    message = getattr(choice, "message", None)
    text = (getattr(message, "content", "") or "").strip()
    if not text:
        raise UserError(_("LLM QC returned empty text."))

    parsed = _parse_qc_response(text)
    parsed["raw_text"] = text
    parsed["probe"] = probe
    return parsed
