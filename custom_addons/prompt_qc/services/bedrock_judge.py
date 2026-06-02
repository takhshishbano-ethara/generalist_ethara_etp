# -*- coding: utf-8 -*-
"""Shared AWS Bedrock judge client for prompt_qc.

This module is the SINGLE code path for talking to Bedrock. It is consumed by:

- the SSE controller (controllers/main.py) for single, browser-driven "Start QC" runs, which
  wraps every event in a `data:` SSE line and streams it to the page, and
- the background worker (models/prompt_qc_run.py `_execute_judge`) for bulk runs, which drains
  the same events server-side via `collect_judge` and persists the result.

The Bedrock converse-stream + amazon-eventstream binary frame parser is ported verbatim from
kensei2/controllers/streaming.py — do not reinvent the eventstream format. The only difference
from Stage 1 is that the streaming call now yields *normalized event tuples* instead of SSE
bytes, so the non-streaming worker can consume them too.
"""
import json
import logging
import os
import random
import struct
import time
from urllib.parse import quote

import httpx

from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)

BEDROCK_CONVERSE_STREAM_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse-stream"
)
DEFAULT_REGION = "ap-south-1"
JUDGE_MAX_TOKENS = 8192
JUDGE_TEMPERATURE = 0.2

# Retry policy for the Bedrock call (ported from gohan/vegeta's bedrock_service). A 429
# (throttling) or a transient 5xx is retried with exponential backoff + jitter; any other status
# fails immediately. Retries happen ONLY before the stream has produced output — once tokens are
# flowing we are committed, because a mid-stream retry would duplicate the verdict.
_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS = (429, 500, 502, 503, 529)
_MAX_BACKOFF = 8.0


def _sleep_backoff(attempt):
    """Exponential backoff with jitter: ~1-2s, then ~2-3s, capped at _MAX_BACKOFF seconds."""
    time.sleep(min((2 ** attempt) + random.random(), _MAX_BACKOFF))


# ----------------------------------------------------------------------
# .env reader (local copy; house convention is to not import across addons).
# ----------------------------------------------------------------------
def _load_dotenv():
    env = os.environ.copy()
    root = None
    try:
        conf_path = odoo_config.get("config") or getattr(odoo_config, "rcfile", None)
    except Exception:
        conf_path = None
    if conf_path:
        root = os.path.dirname(os.path.abspath(conf_path))
    if not root:
        root = os.getcwd()
    dotenv_path = os.path.join(root, ".env")
    if os.path.isfile(dotenv_path):
        try:
            with open(dotenv_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip()
        except Exception:
            _logger.warning("prompt_qc: failed to read .env at %s", dotenv_path)
    return env


def _region_from_arn(arn):
    if not arn:
        return DEFAULT_REGION
    parts = arn.split(":")
    if len(parts) >= 4 and parts[3]:
        return parts[3]
    return DEFAULT_REGION


def get_api_key(ICP):
    """Resolve the Bedrock bearer token: System Parameter first, then .env. `ICP` is a sudo'd
    `ir.config_parameter` recordset (works from a request env or a worker env alike)."""
    key = (ICP.get_param("prompt_qc.bedrock_api_key") or "").strip()
    if key:
        return key
    env = _load_dotenv() or {}
    return (env.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()


def resolve_arn_and_region(ICP):
    arn = (ICP.get_param("prompt_qc.bedrock_inference_arn") or "").strip()
    region = (ICP.get_param("prompt_qc.bedrock_region") or "").strip() or _region_from_arn(arn)
    return arn, region


# ----------------------------------------------------------------------
# amazon-eventstream binary frame parsing (ported verbatim from
# kensei2/controllers/streaming.py — do not reinvent this format).
# ----------------------------------------------------------------------
def _parse_event_headers(data):
    headers = {}
    offset = 0
    n = len(data)
    while offset < n:
        name_len = data[offset]
        offset += 1
        name = data[offset:offset + name_len].decode("utf-8", errors="replace")
        offset += name_len
        if offset >= n:
            break
        vtype = data[offset]
        offset += 1
        if vtype == 7:
            vlen = struct.unpack(">H", data[offset:offset + 2])[0]
            offset += 2
            headers[name] = data[offset:offset + vlen].decode("utf-8", errors="replace")
            offset += vlen
        elif vtype in (0, 1):
            headers[name] = bool(vtype)
        elif vtype == 4:
            vlen = struct.unpack(">H", data[offset:offset + 2])[0]
            offset += 2
            headers[name] = data[offset:offset + vlen]
            offset += vlen
        elif vtype == 5:
            headers[name] = struct.unpack(">i", data[offset:offset + 4])[0]
            offset += 4
        elif vtype in (6, 8):
            headers[name] = struct.unpack(">q", data[offset:offset + 8])[0]
            offset += 8
        elif vtype == 9:
            headers[name] = data[offset:offset + 16]
            offset += 16
        else:
            break
    return headers


def _iter_event_stream(raw_iter):
    buf = b""
    for chunk in raw_iter:
        if not chunk:
            continue
        buf += chunk
        while len(buf) >= 12:
            total_len = struct.unpack(">I", buf[0:4])[0]
            headers_len = struct.unpack(">I", buf[4:8])[0]
            if total_len < 16 or len(buf) < total_len:
                break
            msg = buf[:total_len]
            buf = buf[total_len:]
            headers_data = msg[12:12 + headers_len]
            payload_bytes = msg[12 + headers_len:total_len - 4]
            headers = _parse_event_headers(headers_data)
            event_type = headers.get(":event-type") or headers.get(":exception-type") or ""
            try:
                payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
            except Exception:
                payload = {}
            yield event_type, payload


# ----------------------------------------------------------------------
# Normalized judge event stream.
# ----------------------------------------------------------------------
def iter_judge_events(api_key, inference_arn, region, system_prompt, user_message,
                      rubric_text=None,
                      max_tokens=JUDGE_MAX_TOKENS, temperature=JUDGE_TEMPERATURE,
                      timeout=600.0, max_seconds=None):
    """Yield normalized ``(kind, data)`` events for one Bedrock converse-stream judge call.

    kinds:
      - ("delta",    "<text chunk>")
      - ("metadata", {"input_tokens": int, "output_tokens": int})
      - ("done",     None)                      # emitted ONLY on clean completion
      - ("error",    "<message>")               # non-200, exception, or Bedrock exception frame

    A non-200 / exception yields a single ("error", ...) and returns WITHOUT a ("done", ...) —
    callers gate success on having seen ("done", ...). This mirrors Stage 1 exactly.

    ``max_seconds`` is a TOTAL wall-clock budget. httpx's ``timeout`` is per-operation (a
    slow-trickle stream can run forever without tripping the read timeout), so the background
    worker passes ``max_seconds`` to get a hard end-to-end deadline: once exceeded, the stream is
    abandoned with an ("error", ...). Stage 1's SSE path leaves it None (browser-driven, no
    reaper) so its behaviour is unchanged.

    The rubric `.json` (when present) rides as a SECOND system block — it is judging criteria,
    so it belongs with the judge's instructions (decision D5).
    """
    model_id = quote(inference_arn, safe="")
    url = BEDROCK_CONVERSE_STREAM_URL.format(region=region, model_id=model_id)

    system_blocks = [{"text": system_prompt}]
    if rubric_text and rubric_text.strip():
        system_blocks.append({"text": rubric_text})

    payload = {
        "messages": [{"role": "user", "content": [{"text": user_message}]}],
        "system": system_blocks,
        "inferenceConfig": {"maxTokens": int(max_tokens), "temperature": float(temperature)},
    }
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
        "Accept": "application/vnd.amazon.eventstream",
    }

    start = time.monotonic()
    last_error = None
    for attempt in range(_MAX_ATTEMPTS):
        streamed_any = False  # once True, a failure is terminal — never retry (would duplicate)
        try:
            with httpx.Client(http2=False, timeout=timeout) as client:
                with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code == 200:
                        for event_type, data in _iter_event_stream(resp.iter_raw()):
                            if max_seconds is not None and (time.monotonic() - start) > max_seconds:
                                # Hard wall-clock deadline: abandon the stream (no ("done", ...))
                                # so the caller records a failure and frees its slot in time.
                                yield ("error",
                                       "Judge call exceeded the %ds time budget." % int(max_seconds))
                                return
                            if event_type == "contentBlockDelta":
                                text = (data.get("delta") or {}).get("text") or ""
                                if text:
                                    streamed_any = True
                                    yield ("delta", text)
                            elif event_type == "metadata":
                                usage = data.get("usage") or {}
                                yield ("metadata", {
                                    "input_tokens": int(usage.get("inputTokens", 0)),
                                    "output_tokens": int(usage.get("outputTokens", 0)),
                                })
                        yield ("done", None)
                        return

                    # Non-200: read a bounded slice of the body for the error message.
                    raw = b""
                    for chunk in resp.iter_raw():
                        raw += chunk
                        if len(raw) > 2000:
                            break
                    last_error = "Bedrock %s: %s" % (
                        resp.status_code, raw[:2000].decode("utf-8", errors="replace"))
                    if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS - 1:
                        _logger.warning(
                            "prompt_qc: Bedrock %s (attempt %d/%d) — backing off",
                            resp.status_code, attempt + 1, _MAX_ATTEMPTS)
                        _sleep_backoff(attempt)
                        continue
                    yield ("error", last_error)
                    return
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Connect/read failure. Safe to retry ONLY if no tokens were emitted yet.
            last_error = "Bedrock connection error: %s" % exc
            if not streamed_any and attempt < _MAX_ATTEMPTS - 1:
                _logger.warning(
                    "prompt_qc: %s (attempt %d/%d) — backing off",
                    last_error, attempt + 1, _MAX_ATTEMPTS)
                _sleep_backoff(attempt)
                continue
            yield ("error", last_error)
            return
        except Exception as exc:
            _logger.exception("prompt_qc: Bedrock stream failed")
            yield ("error", str(exc))
            return

    yield ("error", last_error or "Bedrock: all retries exhausted")


def collect_judge(api_key, inference_arn, region, system_prompt, user_message,
                  rubric_text=None,
                  max_tokens=JUDGE_MAX_TOKENS, temperature=JUDGE_TEMPERATURE,
                  timeout=600.0, max_seconds=None, on_delta=None):
    """Drain ``iter_judge_events`` server-side and return the full result.

    Returns a dict: {"text": str, "usage": {...}, "ok": bool, "error": str|None}. ``ok`` is True
    only when a ("done", ...) event was seen and no ("error", ...) occurred — the same
    persist-only-on-completion contract Stage 1 enforces. ``max_seconds`` is the total wall-clock
    budget the background worker enforces on the call.

    ``on_delta`` is an optional callback invoked with each text chunk as it arrives, so the worker
    can live-stream the verdict (e.g. over the bus) while still accumulating it for the single
    persisted result. A failing callback is logged and ignored — it must never break the call.
    """
    accumulated = ""
    usage = {}
    completed = False
    error = None
    for kind, data in iter_judge_events(
        api_key, inference_arn, region, system_prompt, user_message,
        rubric_text=rubric_text, max_tokens=max_tokens, temperature=temperature,
        timeout=timeout, max_seconds=max_seconds,
    ):
        if kind == "delta":
            accumulated += data
            if on_delta is not None:
                try:
                    on_delta(data)
                except Exception:
                    _logger.exception("prompt_qc: on_delta callback failed")
        elif kind == "metadata":
            usage = data or {}
        elif kind == "done":
            completed = True
        elif kind == "error":
            error = (data or "")[:1000]
    return {
        "text": accumulated,
        "usage": usage,
        "ok": completed and not error,
        "error": error,
    }
