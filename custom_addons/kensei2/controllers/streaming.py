# -*- coding: utf-8 -*-
"""SSE streaming endpoints for kensei2 LLM-driven features.

Three POST endpoints under /kensei2/:
  - golden/stream         live golden trajectory generation
  - rubric_overlap/stream live rubric vs test overlap audit
  - rubric_eval/stream    parallel rubric pass/fail evaluation with progress events

All endpoints use AWS Bedrock Converse-Stream over httpx with Bearer auth, mirroring
the pattern in skoll_project/controllers/golden_generation.py. DB writes happen in
the `finally` block via Registry().cursor() so the cursor is not held across the stream.
"""
import copy
import json
import logging
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote

import httpx
from werkzeug.wrappers import Response as WerkzeugResponse

from odoo import api, http, SUPERUSER_ID
from odoo.http import request
from odoo.modules.module import get_module_path
from odoo.modules.registry import Registry

from ..models.kensei2 import (
    _get_golden_prompt,
    _get_rubric_eval_prompt,
    _get_rubric_overlap_prompt,
    _parse_trajectory_entries,
    _extract_messages_from_entry,
    _serialise_messages_for_llm,
    _load_dotenv,
)

_logger = logging.getLogger(__name__)

BEDROCK_CONVERSE_URL = "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
BEDROCK_CONVERSE_STREAM_URL = "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse-stream"
GOLDEN_MAX_TOKENS = 65536
OVERLAP_MAX_TOKENS = 8192
RUBRIC_EVAL_MAX_TOKENS = 4096
RUBRIC_EVAL_CONCURRENCY = 4
DEFAULT_REGION = "ap-south-1"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _region_from_arn(arn):
    if not arn:
        return DEFAULT_REGION
    parts = arn.split(":")
    if len(parts) >= 4 and parts[3]:
        return parts[3]
    return DEFAULT_REGION


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


def _sse_line(data):
    return ("data: %s\n\n" % json.dumps(data, ensure_ascii=False)).encode("utf-8")


def _sse_error_response(message):
    body = _sse_line({"type": "error", "message": message}) + _sse_line({"type": "complete", "status": "error"})
    return WerkzeugResponse(body, mimetype="text/event-stream", headers=SSE_HEADERS, direct_passthrough=True)


def _get_api_key():
    env = _load_dotenv() or {}
    return (env.get("KENSEI2_AWS_BEARER_TOKEN") or env.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()


def _resolve_arn_and_region(ICP):
    """Returns (inference_arn, region). test_gen ARN falls back to bedrock_inference_arn."""
    arn = (ICP.get_param("kensei2.test_gen_inference_arn") or "").strip()
    if not arn:
        arn = (ICP.get_param("kensei2.bedrock_inference_arn") or "").strip()
    region = (ICP.get_param("kensei2.bedrock_region") or "").strip() or _region_from_arn(arn)
    return arn, region


def _stream_bedrock_sse(api_key, inference_arn, region, system_prompt, user_message,
                       max_tokens=GOLDEN_MAX_TOKENS, temperature=0.7, timeout=600.0,
                       thinking_budget=0):
    """Generator yielding SSE-encoded bytes for a Bedrock Converse-Stream call."""
    model_id = quote(inference_arn, safe="")
    url = BEDROCK_CONVERSE_STREAM_URL.format(region=region, model_id=model_id)

    inference_config = {}
    inference_config["maxTokens"] = int(max_tokens)
    if thinking_budget > 0:
        # Claude requires temperature=1 when extended thinking is enabled.
        inference_config["temperature"] = 1
    elif temperature is not None:
        inference_config["temperature"] = float(temperature)

    payload = {
        "messages": [{"role": "user", "content": [{"text": user_message}]}],
        "system": [{"text": system_prompt}],
        "inferenceConfig": inference_config,
    }
    if thinking_budget > 0:
        payload["additionalModelRequestFields"] = {
            "thinking": {"type": "enabled", "budget_tokens": int(thinking_budget)}
        }

    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
        "Accept": "application/vnd.amazon.eventstream",
    }

    try:
        with httpx.Client(http2=False, timeout=timeout) as client:
            with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    raw = b""
                    for chunk in resp.iter_raw():
                        raw += chunk
                        if len(raw) > 2000:
                            break
                    yield _sse_line({
                        "type": "error",
                        "message": "Bedrock %s: %s" % (
                            resp.status_code,
                            raw[:2000].decode("utf-8", errors="replace"),
                        ),
                    })
                    return

                for event_type, data in _iter_event_stream(resp.iter_raw()):
                    if event_type == "contentBlockDelta":
                        delta = data.get("delta") or {}
                        text = delta.get("text") or ""
                        if text:
                            yield _sse_line({"type": "delta", "text": text})
                    elif event_type == "messageStop":
                        yield _sse_line({"type": "stop", "stopReason": data.get("stopReason")})
                    elif event_type == "metadata":
                        usage = data.get("usage") or {}
                        yield _sse_line({
                            "type": "metadata",
                            "usage": {
                                "input_tokens": int(usage.get("inputTokens", 0)),
                                "output_tokens": int(usage.get("outputTokens", 0)),
                            },
                        })
                yield _sse_line({"type": "done"})
    except Exception as exc:
        _logger.exception("Bedrock stream failed")
        yield _sse_line({"type": "error", "message": str(exc)})


def _bedrock_converse_sync(api_key, inference_arn, region, system_prompt, user_message,
                          max_tokens=RUBRIC_EVAL_MAX_TOKENS, temperature=0.2, timeout=300.0):
    """Synchronous (non-streaming) Bedrock Converse call. Returns (text, usage_dict)."""
    model_id = quote(inference_arn, safe="")
    url = BEDROCK_CONVERSE_URL.format(region=region, model_id=model_id)
    payload = {
        "messages": [{"role": "user", "content": [{"text": user_message}]}],
        "system": [{"text": system_prompt}],
        "inferenceConfig": {"maxTokens": int(max_tokens), "temperature": float(temperature)},
    }
    headers = {"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json"}
    with httpx.Client(http2=False, timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError("Bedrock %s: %s" % (resp.status_code, resp.text[:500]))
        data = resp.json()
        text_blocks = []
        for block in (data.get("output", {}).get("message", {}).get("content", []) or []):
            if "text" in block:
                text_blocks.append(block["text"])
        text = "".join(text_blocks)
        usage = data.get("usage") or {}
        return text, {
            "input_tokens": int(usage.get("inputTokens", 0)),
            "output_tokens": int(usage.get("outputTokens", 0)),
        }


def _write_golden_result(db_name, task_id, accumulated, usage, status, error):
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(int(task_id))
            if not task.exists():
                return
            usage = usage or {}
            vals = {
                "golden_status": status,
                "golden_error": (error or False) if status == "error" else False,
                "golden_input_tokens": (task.golden_input_tokens or 0) + int(usage.get("input_tokens", 0)),
                "golden_output_tokens": (task.golden_output_tokens or 0) + int(usage.get("output_tokens", 0)),
            }
            if status == "done" and accumulated:
                vals["golden_trajectory"] = accumulated
            task.write(vals)
    except Exception:
        _logger.exception("Failed to persist golden stream result for task %s", task_id)


def _write_overlap_result(db_name, task_id, accumulated, usage, status, error):
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(int(task_id))
            if not task.exists():
                return
            usage = usage or {}
            vals = {
                "rubric_test_overlap_status": status,
                "rubric_test_overlap_error": (error or False) if status == "error" else False,
                "rubric_test_overlap_input_tokens": (task.rubric_test_overlap_input_tokens or 0) + int(usage.get("input_tokens", 0)),
                "rubric_test_overlap_output_tokens": (task.rubric_test_overlap_output_tokens or 0) + int(usage.get("output_tokens", 0)),
            }
            if status == "done" and accumulated:
                vals["rubric_test_overlap_report"] = accumulated
            task.write(vals)
    except Exception:
        _logger.exception("Failed to persist overlap stream result for task %s", task_id)


def _write_rubric_eval_result(db_name, task_id, rubrics_json, usage, status, error):
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(int(task_id))
            if not task.exists():
                return
            usage = usage or {}
            vals = {
                "rubric_eval_status": status,
                "rubric_eval_error": (error or False) if status == "error" else False,
                "rubric_eval_input_tokens": (task.rubric_eval_input_tokens or 0) + int(usage.get("input_tokens", 0)),
                "rubric_eval_output_tokens": (task.rubric_eval_output_tokens or 0) + int(usage.get("output_tokens", 0)),
            }
            if status == "done" and rubrics_json is not None:
                vals["rubrics"] = rubrics_json
            task.write(vals)
    except Exception:
        _logger.exception("Failed to persist rubric eval stream result for task %s", task_id)


def _parse_rubrics(raw):
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("rubrics"), list):
        return parsed["rubrics"]
    return None


def _ensure_verdict_arrays(rubric):
    for key in ("claude", "gpt"):
        arr = rubric.get(key)
        if not isinstance(arr, list):
            arr = []
        if len(arr) < 12:
            arr = arr + [None] * (12 - len(arr))
        rubric[key] = arr


def _strip_code_fences(text):
    if not text:
        return ""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _build_golden_user_message(task):
    persona = task.persona_id
    soul_md = (persona.soul_md if persona else "") or ""
    memory_md = (persona.memory_md if persona else "") or ""
    agents_md = (persona.agents_md if persona else "") or ""

    delivery_schema = ""
    try:
        schema_path = os.path.join(get_module_path("kensei2"), "Delivery_Schema.json")
        if os.path.isfile(schema_path):
            with open(schema_path, "r", encoding="utf-8") as fh:
                delivery_schema = fh.read()
    except Exception:
        _logger.warning("Could not load Delivery_Schema.json")

    sections = []
    if task.claude_trajectory:
        sections.append("## Model Trajectory 1 (Claude)\n%s" % task.claude_trajectory)
    glm_traj = getattr(task, "glm_trajectory", None) or ""
    if glm_traj:
        sections.append("## Model Trajectory 2 (GLM)\n%s" % glm_traj)
    if not sections and task.gpt_trajectory:
        sections.append("## Model Trajectory 1 (GPT)\n%s" % task.gpt_trajectory)
    trajectories_block = "\n\n".join(sections)

    return (
        "## Current Date\n%s\n\n"
        "## Delivery Schema\n```json\n%s\n```\n\n"
        "## SOUL.md\n%s\n\n## MEMORY.md\n%s\n\n## AGENTS.md\n%s\n\n%s"
    ) % (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        delivery_schema,
        soul_md,
        memory_md,
        agents_md,
        trajectories_block,
    )


def _build_overlap_user_message(task, test_code):
    parts = []
    if task.initial_prompt:
        parts.append("## Task Instruction\n%s" % task.initial_prompt)
    parts.append("## rubrics.json\n```json\n%s\n```" % (task.rubrics or "[]"))
    parts.append("## test_outputs.py\n```python\n%s\n```" % test_code)
    if task.test_weights:
        parts.append("## test_weights.json\n```json\n%s\n```" % task.test_weights)
    return "\n\n".join(parts)


def _build_rubric_eval_user_message(task, model_name, run_idx, messages, rubric_labels):
    traj_text = _serialise_messages_for_llm(messages, max_chars=200000)
    parts = [
        "## Task Instruction\n%s" % (task.initial_prompt or ""),
        "## Model: %s  (run #%d)" % (model_name, run_idx + 1),
        "## Rubric Criteria (return verdicts in this exact order)\n%s" % json.dumps(rubric_labels, indent=2),
        "## Trajectory Messages\n```json\n%s\n```" % traj_text,
    ]
    return "\n\n".join(parts)


def _eval_single_trajectory(api_key, inference_arn, region, system_prompt,
                            task, model_key, run_idx, entry, rubric_labels):
    """Run one Bedrock call to judge pass/fail for all rubrics against one trajectory.

    Returns a dict: {model: model_key, run: run_idx, verdicts: [...], usage: {...}, error: ''}
    On any failure to parse, defaults each label to 'fail' (security policy).
    """
    messages = _extract_messages_from_entry(entry)
    if not messages:
        return {
            "model": model_key, "run": run_idx,
            "verdicts": ["fail"] * len(rubric_labels),
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "error": "empty trajectory",
        }

    model_name = "Claude" if model_key == "claude" else "GPT"
    user_message = _build_rubric_eval_user_message(task, model_name, run_idx, messages, rubric_labels)

    try:
        text, usage = _bedrock_converse_sync(
            api_key, inference_arn, region, system_prompt, user_message,
            max_tokens=RUBRIC_EVAL_MAX_TOKENS, temperature=0.2, timeout=300.0,
        )
    except Exception as exc:
        return {
            "model": model_key, "run": run_idx,
            "verdicts": ["fail"] * len(rubric_labels),
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "error": str(exc)[:300],
        }

    cleaned = _strip_code_fences(text)
    parsed = None
    try:
        parsed = json.loads(cleaned)
    except Exception:
        import re as _re
        m = _re.search(r"\[.*\]", cleaned, _re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                parsed = None

    verdict_by_label = {}
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            label = item.get("label") or item.get("criterion") or item.get("name")
            verdict = item.get("verdict") or item.get("result") or item.get("pass")
            if isinstance(verdict, bool):
                verdict = "pass" if verdict else "fail"
            elif isinstance(verdict, str):
                v = verdict.strip().lower()
                verdict = "pass" if v in ("pass", "passed", "true", "yes") else "fail"
            else:
                verdict = "fail"
            if label:
                verdict_by_label[label] = verdict

    aligned = [verdict_by_label.get(lbl, "fail") for lbl in rubric_labels]
    return {
        "model": model_key, "run": run_idx,
        "verdicts": aligned,
        "usage": usage,
        "error": "" if parsed is not None else "parse_failed_defaulted_fail",
    }


class Kensei2StreamingController(http.Controller):

    @http.route("/kensei2/golden/stream", type="http", auth="user", methods=["POST"], csrf=False)
    def golden_stream(self, **_kw):
        ICP = request.env["ir.config_parameter"].sudo()
        if ICP.get_param("kensei2.disable_golden_streaming") in ("True", "true", "1"):
            return _sse_error_response("Golden streaming is disabled in Settings.")

        try:
            body = json.loads(request.httprequest.data or b"{}")
        except Exception:
            body = {}
        record_id = body.get("record_id")
        if not record_id:
            return _sse_error_response("Missing record_id.")

        api_key = _get_api_key()
        if not api_key:
            return _sse_error_response("Missing Bedrock API key in .env.")
        inference_arn, region = _resolve_arn_and_region(ICP)
        if not inference_arn:
            return _sse_error_response("Missing kensei2.test_gen_inference_arn / kensei2.bedrock_inference_arn.")

        task = request.env["kensei2.kensei2"].sudo().browse(int(record_id))
        if not task.exists():
            return _sse_error_response("Task %s not found." % record_id)

        if not (task.claude_trajectory or task.gpt_trajectory or getattr(task, "glm_trajectory", None)):
            return _sse_error_response("Need at least one trajectory (Claude/GPT/GLM) before generating golden.")

        try:
            system_prompt = _get_golden_prompt()
        except Exception as exc:
            return _sse_error_response("Failed to load golden_prompt.md: %s" % exc)

        try:
            user_message = _build_golden_user_message(task)
        except Exception as exc:
            return _sse_error_response("Failed to build prompt: %s" % exc)

        task.write({"golden_status": "generating", "golden_error": False})

        db_name = request.env.cr.dbname
        task_db_id = task.id

        def _logged_stream():
            t0 = time.time()
            accumulated = ""
            usage = {}
            status = "done"
            error = None
            try:
                for chunk in _stream_bedrock_sse(
                    api_key, inference_arn, region, system_prompt, user_message,
                    max_tokens=GOLDEN_MAX_TOKENS, temperature=0.7, timeout=1200.0,
                ):
                    yield chunk
                    try:
                        line = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                        payload = json.loads(line.split("data: ", 1)[1].split("\n")[0])
                        ptype = payload.get("type")
                        if ptype == "delta":
                            accumulated += payload.get("text", "")
                        elif ptype == "metadata":
                            usage = payload.get("usage", {}) or {}
                        elif ptype == "error":
                            status = "error"
                            error = (payload.get("message") or "")[:1000]
                    except (IndexError, json.JSONDecodeError, KeyError, UnicodeDecodeError):
                        pass
            except Exception as exc:
                _logger.exception("Golden stream loop failed")
                status = "error"
                error = str(exc)[:1000]
                yield _sse_line({"type": "error", "message": error})
            finally:
                duration = round(time.time() - t0, 2)
                _write_golden_result(db_name, task_db_id, accumulated, usage, status, error)
                yield _sse_line({"type": "complete", "status": status, "duration": duration})

        return WerkzeugResponse(
            _logged_stream(),
            mimetype="text/event-stream",
            headers=SSE_HEADERS,
            direct_passthrough=True,
        )

    @http.route("/kensei2/rubric_overlap/stream", type="http", auth="user", methods=["POST"], csrf=False)
    def rubric_overlap_stream(self, **_kw):
        ICP = request.env["ir.config_parameter"].sudo()
        if ICP.get_param("kensei2.disable_rubric_overlap_check") in ("True", "true", "1"):
            return _sse_error_response("Rubric overlap check is disabled in Settings.")

        try:
            body = json.loads(request.httprequest.data or b"{}")
        except Exception:
            body = {}
        record_id = body.get("record_id")
        if not record_id:
            return _sse_error_response("Missing record_id.")

        api_key = _get_api_key()
        if not api_key:
            return _sse_error_response("Missing Bedrock API key in .env.")
        inference_arn, region = _resolve_arn_and_region(ICP)
        if not inference_arn:
            return _sse_error_response("Missing kensei2.test_gen_inference_arn / kensei2.bedrock_inference_arn.")

        task = request.env["kensei2.kensei2"].sudo().browse(int(record_id))
        if not task.exists():
            return _sse_error_response("Task %s not found." % record_id)

        if not task.rubrics:
            return _sse_error_response("Rubrics are empty. Define rubrics before running overlap audit.")

        try:
            test_code = task._get_best_test_code() if hasattr(task, "_get_best_test_code") else (task.test_code or "")
        except Exception:
            test_code = task.test_code or ""
        if not test_code:
            return _sse_error_response("No test code available. Generate tests before running overlap audit.")

        try:
            system_prompt = _get_rubric_overlap_prompt()
        except Exception as exc:
            return _sse_error_response("Failed to load rubric_test_overlap_prompt.md: %s" % exc)

        user_message = _build_overlap_user_message(task, test_code)
        task.write({"rubric_test_overlap_status": "generating", "rubric_test_overlap_error": False})

        db_name = request.env.cr.dbname
        task_db_id = task.id

        def _logged_stream():
            t0 = time.time()
            accumulated = ""
            usage = {}
            status = "done"
            error = None
            try:
                for chunk in _stream_bedrock_sse(
                    api_key, inference_arn, region, system_prompt, user_message,
                    max_tokens=OVERLAP_MAX_TOKENS, temperature=0.2, timeout=300.0,
                ):
                    yield chunk
                    try:
                        line = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                        payload = json.loads(line.split("data: ", 1)[1].split("\n")[0])
                        ptype = payload.get("type")
                        if ptype == "delta":
                            accumulated += payload.get("text", "")
                        elif ptype == "metadata":
                            usage = payload.get("usage", {}) or {}
                        elif ptype == "error":
                            status = "error"
                            error = (payload.get("message") or "")[:1000]
                    except (IndexError, json.JSONDecodeError, KeyError, UnicodeDecodeError):
                        pass
            except Exception as exc:
                _logger.exception("Overlap stream loop failed")
                status = "error"
                error = str(exc)[:1000]
                yield _sse_line({"type": "error", "message": error})
            finally:
                duration = round(time.time() - t0, 2)
                _write_overlap_result(db_name, task_db_id, accumulated, usage, status, error)
                yield _sse_line({"type": "complete", "status": status, "duration": duration})

        return WerkzeugResponse(
            _logged_stream(),
            mimetype="text/event-stream",
            headers=SSE_HEADERS,
            direct_passthrough=True,
        )

    @http.route("/kensei2/rubric_eval/stream", type="http", auth="user", methods=["POST"], csrf=False)
    def rubric_eval_stream(self, **_kw):
        ICP = request.env["ir.config_parameter"].sudo()
        if ICP.get_param("kensei2.disable_rubric_evaluation") in ("True", "true", "1"):
            return _sse_error_response("Rubric evaluation is disabled in Settings.")

        try:
            body = json.loads(request.httprequest.data or b"{}")
        except Exception:
            body = {}
        record_id = body.get("record_id")
        if not record_id:
            return _sse_error_response("Missing record_id.")

        api_key = _get_api_key()
        if not api_key:
            return _sse_error_response("Missing Bedrock API key in .env.")
        inference_arn, region = _resolve_arn_and_region(ICP)
        if not inference_arn:
            return _sse_error_response("Missing kensei2.test_gen_inference_arn / kensei2.bedrock_inference_arn.")

        task = request.env["kensei2.kensei2"].sudo().browse(int(record_id))
        if not task.exists():
            return _sse_error_response("Task %s not found." % record_id)

        rubrics = _parse_rubrics(task.rubrics)
        if not rubrics:
            return _sse_error_response("Rubrics are empty or unparseable.")

        rubric_labels = [r.get("label", "") for r in rubrics if isinstance(r, dict)]
        if not rubric_labels:
            return _sse_error_response("No rubric labels found.")

        claude_entries = _parse_trajectory_entries(task.claude_trajectory)
        gpt_entries = _parse_trajectory_entries(task.gpt_trajectory)
        if not claude_entries and not gpt_entries:
            return _sse_error_response("Need at least one Claude or GPT trajectory.")

        try:
            system_prompt = _get_rubric_eval_prompt()
        except Exception as exc:
            return _sse_error_response("Failed to load rubric_evaluation_system_prompt.md: %s" % exc)

        jobs = []
        for idx, entry in enumerate(claude_entries[:8]):
            jobs.append(("claude", idx, entry))
        for idx, entry in enumerate(gpt_entries[:8]):
            jobs.append(("gpt", idx, entry))
        total_jobs = len(jobs)

        task.write({"rubric_eval_status": "generating", "rubric_eval_error": False})

        # Snapshot for thread workers (avoid using `task` recordset across threads).
        task_snapshot = {
            "initial_prompt": task.initial_prompt or "",
        }

        class _TaskShim:
            def __init__(self, data):
                self.initial_prompt = data["initial_prompt"]

        task_shim = _TaskShim(task_snapshot)

        db_name = request.env.cr.dbname
        task_db_id = task.id
        original_rubrics = copy.deepcopy(rubrics)

        def _logged_stream():
            t0 = time.time()
            total_usage = {"input_tokens": 0, "output_tokens": 0}
            results = []
            errors = []
            status = "done"
            error = None
            completed = 0
            rubrics_json = None
            try:
                yield _sse_line({"type": "start", "total_jobs": total_jobs, "rubric_count": len(rubric_labels)})
                concurrency = max(1, min(RUBRIC_EVAL_CONCURRENCY, total_jobs))
                with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="kensei2-rubric-eval") as pool:
                    futures = {
                        pool.submit(
                            _eval_single_trajectory,
                            api_key, inference_arn, region, system_prompt,
                            task_shim, model_key, run_idx, entry, rubric_labels,
                        ): (model_key, run_idx)
                        for (model_key, run_idx, entry) in jobs
                    }
                    for fut in as_completed(futures):
                        model_key, run_idx = futures[fut]
                        try:
                            res = fut.result()
                        except Exception as exc:
                            errors.append("%s#%d: %s" % (model_key, run_idx, exc))
                            res = {
                                "model": model_key, "run": run_idx,
                                "verdicts": ["fail"] * len(rubric_labels),
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                                "error": str(exc)[:300],
                            }
                        results.append(res)
                        total_usage["input_tokens"] += int(res["usage"].get("input_tokens", 0))
                        total_usage["output_tokens"] += int(res["usage"].get("output_tokens", 0))
                        completed += 1
                        if res.get("error"):
                            errors.append("%s#%d: %s" % (model_key, run_idx, res["error"]))
                            yield _sse_line({
                                "type": "job_error",
                                "model": model_key, "run": run_idx,
                                "completed": completed, "total": total_jobs,
                                "error": res["error"],
                                "verdicts": res["verdicts"],
                            })
                        else:
                            yield _sse_line({
                                "type": "job_complete",
                                "model": model_key, "run": run_idx,
                                "completed": completed, "total": total_jobs,
                                "verdicts": res["verdicts"],
                            })

                merged = copy.deepcopy(original_rubrics)
                for r in merged:
                    if isinstance(r, dict):
                        _ensure_verdict_arrays(r)
                for res in results:
                    model_key = res["model"]
                    run_idx = res["run"]
                    verdicts = res["verdicts"]
                    for r_idx, rubric in enumerate(merged):
                        if not isinstance(rubric, dict):
                            continue
                        if r_idx < len(verdicts) and 0 <= run_idx < 12:
                            rubric[model_key][run_idx] = verdicts[r_idx]
                rubrics_json = json.dumps(merged, ensure_ascii=False)

                if errors:
                    error = "Partial failures: %s" % "; ".join(errors[:5])
                yield _sse_line({
                    "type": "done",
                    "completed": completed,
                    "total": total_jobs,
                    "usage": total_usage,
                    "errors": len(errors),
                })
            except Exception as exc:
                _logger.exception("Rubric eval stream failed")
                status = "error"
                error = str(exc)[:1000]
                yield _sse_line({"type": "error", "message": error})
            finally:
                duration = round(time.time() - t0, 2)
                _write_rubric_eval_result(
                    db_name, task_db_id,
                    rubrics_json if status == "done" else None,
                    total_usage, status, error,
                )
                yield _sse_line({"type": "complete", "status": status, "duration": duration})

        return WerkzeugResponse(
            _logged_stream(),
            mimetype="text/event-stream",
            headers=SSE_HEADERS,
            direct_passthrough=True,
        )
