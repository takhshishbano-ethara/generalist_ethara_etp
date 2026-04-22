# -*- coding: utf-8 -*-
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import httpx

from odoo import api, http, SUPERUSER_ID
from odoo.http import request
from odoo.modules.module import get_module_path
from odoo.modules.registry import Registry

from ..models.talos import _load_dotenv

_logger = logging.getLogger(__name__)

BEDROCK_CONVERSE_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
)

_system_prompt_cache = {"text": None, "mtime": 0}
_trajectory_qc_prompt_cache = {"text": None, "mtime": 0}
_golden_trajectory_qc_prompt_cache = {"text": None, "mtime": 0}


def _get_system_prompt():
    mod_path = get_module_path("talos")
    if not mod_path:
        return ""

    path = os.path.join(mod_path, "system_prompts.md")
    if not os.path.isfile(path):
        return ""

    mtime = os.path.getmtime(path)
    if (
        _system_prompt_cache["text"] is not None
        and _system_prompt_cache["mtime"] == mtime
    ):
        return _system_prompt_cache["text"]

    with open(path, "r") as f:
        _system_prompt_cache["text"] = f.read().strip()
    _system_prompt_cache["mtime"] = mtime
    return _system_prompt_cache["text"]


def _get_trajectory_qc_prompt():
    mod_path = get_module_path("talos")
    if not mod_path:
        return ""

    path = os.path.join(mod_path, "trajectory_qc_prompt.md")
    if not os.path.isfile(path):
        return ""

    mtime = os.path.getmtime(path)
    if (
        _trajectory_qc_prompt_cache["text"] is not None
        and _trajectory_qc_prompt_cache["mtime"] == mtime
    ):
        return _trajectory_qc_prompt_cache["text"]

    with open(path, "r") as f:
        _trajectory_qc_prompt_cache["text"] = f.read().strip()
    _trajectory_qc_prompt_cache["mtime"] = mtime
    return _trajectory_qc_prompt_cache["text"]


def _get_golden_trajectory_qc_prompt():
    """Return the base QC prompt with golden-specific addendum appended."""
    base = _get_trajectory_qc_prompt()
    if not base:
        return ""

    mod_path = get_module_path("talos")
    if not mod_path:
        return base

    path = os.path.join(mod_path, "golden_trajectory_qc_prompt.md")
    if not os.path.isfile(path):
        return base

    mtime = os.path.getmtime(path)
    if (
        _golden_trajectory_qc_prompt_cache["text"] is not None
        and _golden_trajectory_qc_prompt_cache["mtime"] == mtime
    ):
        addendum = _golden_trajectory_qc_prompt_cache["text"]
    else:
        with open(path, "r") as f:
            addendum = f.read().strip()
        _golden_trajectory_qc_prompt_cache["text"] = addendum
        _golden_trajectory_qc_prompt_cache["mtime"] = mtime

    return base + "\n\n---\n\n" + addendum


def _parse_json_response(text):
    cleaned = text.strip()
    json_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
    if json_block:
        cleaned = json_block.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _parse_qc_verdict(text):
    """Parse the machine-readable JSON block from the QC response.

    The LLM outputs a JSON code block with severity, summary, and per-check
    results.  We extract the JSON, validate the severity field, and return a
    normalized dict the frontend can consume directly.
    """
    parsed = _parse_json_response(text)
    if not parsed or not isinstance(parsed, dict):
        return None

    severity = (parsed.get("severity") or "").strip().lower()
    valid_severities = ("low", "medium", "high", "critical")
    if severity not in valid_severities:
        # Fallback: try to infer from old-style OVERALL VERDICT if present
        verdict_match = re.search(
            r"OVERALL\s+(?:VERDICT|SEVERITY):\s*(\S+)", text, re.IGNORECASE
        )
        if verdict_match:
            raw = verdict_match.group(1).strip().lower()
            if raw in valid_severities:
                severity = raw
            elif raw == "pass":
                severity = "low"
            elif raw == "warn":
                severity = "medium"
            elif raw == "fail":
                severity = "critical"
            else:
                severity = "medium"
        else:
            severity = "medium"

    total_fails = int(parsed.get("total_fails", 0))
    total_warns = int(parsed.get("total_warns", 0))
    total_passes = int(parsed.get("total_passes", 0))

    return {
        "severity": severity,
        "summary": parsed.get("summary", ""),
        "total_fails": total_fails,
        "total_warns": total_warns,
        "total_passes": total_passes,
        "checks": parsed.get("checks", []),
    }


def _is_degenerate(text, threshold=0.8):
    """Detect repetitive token degeneration (e.g. '!!!...', 'aaa...').

    Returns True when any single character makes up more than *threshold*
    of the response — a strong signal the model collapsed into a loop.
    """
    if not text or len(text) < 20:
        return False
    from collections import Counter

    counts = Counter(text)
    most_common_count = counts.most_common(1)[0][1]
    return most_common_count / len(text) >= threshold


_QC_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="traj_qc")


def _run_trajectory_qc_background(
    db_name, record_id, field_name, entry_index, trajectory_str
):
    from .trajectory_qc_validator import validate_trajectory, build_report

    try:
        t0 = time.monotonic()
        label = f"record={record_id}/field={field_name}/entry={entry_index}"
        _logger.info("QC bg: running deterministic validation for %s", label)

        checks = validate_trajectory(label, trajectory_str)
        qc_verdict = build_report(label, checks)
        elapsed = time.monotonic() - t0

        _logger.info(
            "QC bg: deterministic validation done for %s "
            "elapsed=%.2fs severity=%s fails=%d warns=%d passes=%d",
            label,
            elapsed,
            qc_verdict.get("severity"),
            qc_verdict.get("total_fails", 0),
            qc_verdict.get("total_warns", 0),
            qc_verdict.get("total_passes", 0),
        )

        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            _update_qc_entry(
                env, record_id, field_name, entry_index, "done", qc_verdict
            )
    except Exception:
        _logger.exception(
            "QC bg: unhandled error for record=%s field=%s entry=%s",
            record_id,
            field_name,
            entry_index,
        )
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                _update_qc_entry(env, record_id, field_name, entry_index, "error", None)
        except Exception:
            _logger.exception("QC bg: failed to write error status")


def _update_qc_entry(env, record_id, field_name, entry_index, qc_status, qc_result):
    """Write qc_status and qc_result into a specific trajectory entry in DB."""
    try:
        task = env["talos.talos"].browse(record_id)
        if not task.exists():
            return
        raw = task[field_name] or ""
        if not raw.strip():
            return
        data = json.loads(raw)
        entries = data if isinstance(data, list) else [data]
        if entry_index < 0 or entry_index >= len(entries):
            return
        if entries[entry_index].get("qc_status") == "aborted":
            return
        entries[entry_index]["qc_status"] = qc_status
        if qc_result:
            entries[entry_index]["qc_result"] = qc_result
        elif "qc_result" in entries[entry_index]:
            del entries[entry_index]["qc_result"]
        new_value = json.dumps(data, indent=2, ensure_ascii=False)
        task.write({field_name: new_value})
    except Exception:
        _logger.exception(
            "_update_qc_entry failed for record=%s field=%s entry=%s",
            record_id,
            field_name,
            entry_index,
        )


def _accumulate_tokens(env, record_id, in_field, out_field, usage):
    """Add usage tokens to existing task-level counters."""
    try:
        t_in = usage.get("input_tokens", 0)
        t_out = usage.get("output_tokens", 0)
        if t_in <= 0 and t_out <= 0:
            return
        task = env["talos.talos"].browse(record_id)
        if not task.exists():
            return
        task.write(
            {
                in_field: (getattr(task, in_field, 0) or 0) + t_in,
                out_field: (getattr(task, out_field, 0) or 0) + t_out,
            }
        )
    except Exception:
        _logger.exception(
            "_accumulate_tokens failed for record=%s %s/%s",
            record_id,
            in_field,
            out_field,
        )


def _call_bedrock_converse(
    api_key,
    inference_arn,
    region,
    system_prompt,
    user_message,
    max_tokens=4096,
    temperature=0.7,
    top_p=0.9,
    timeout=600.0,
):
    url = BEDROCK_CONVERSE_URL.format(
        region=region,
        model_id=quote(inference_arn, safe=""),
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % api_key,
    }

    payload = {
        "messages": [
            {
                "role": "user",
                "content": [{"text": user_message}],
            },
        ],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
            "topP": top_p,
        },
    }
    if system_prompt:
        payload["system"] = [{"text": system_prompt}]

    with httpx.Client(http2=True, timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        error_detail = resp.text[:500]
        _logger.error(
            "Bedrock API returned status %d: %s", resp.status_code, error_detail
        )
        raise RuntimeError(
            "Bedrock API error (HTTP %d): %s" % (resp.status_code, error_detail)
        )

    result = resp.json()

    output_key = "output" if "output" in result else "Output"
    if output_key in result and isinstance(result[output_key], dict):
        err_type = result[output_key].get("__type", "")
        if err_type:
            raise RuntimeError("Bedrock service error: %s" % err_type)

    content_blocks = result.get("output", {}).get("message", {}).get("content", [])
    response_text = ""
    for block in content_blocks:
        if isinstance(block, dict) and "text" in block:
            response_text += block["text"]

    usage_raw = result.get("usage", {})
    usage = {
        "input_tokens": int(usage_raw.get("inputTokens", 0)),
        "output_tokens": int(usage_raw.get("outputTokens", 0)),
    }

    return response_text.strip(), usage


class LlmAssistQc(http.Controller):
    @http.route("/talos/qc", type="json", auth="user")
    def qc_prompt(
        self, prompt="", system_prompt="", max_tokens=4096, temperature=0.3, **kw
    ):
        prompt = (prompt or "").strip()
        if not prompt:
            return {"error": "prompt is required"}

        env = _load_dotenv()
        api_key = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        if not api_key:
            return {"error": "AWS_BEARER_TOKEN_BEDROCK not set in .env"}

        ICP = request.env["ir.config_parameter"].sudo()
        inference_arn = (ICP.get_param("talos.bedrock_inference_arn") or "").strip()
        region = (ICP.get_param("talos.bedrock_region") or "ap-south-1").strip()

        if not inference_arn:
            return {"error": "Bedrock Inference ARN not configured in Settings > Talos"}

        if not system_prompt:
            system_prompt = _get_system_prompt()

        try:
            _logger.info(
                "seed QC: calling Kimi arn=%s region=%s prompt_len=%d",
                inference_arn,
                region,
                len(prompt),
            )
            t0 = time.monotonic()
            response_text, usage = _call_bedrock_converse(
                api_key=api_key,
                inference_arn=inference_arn,
                region=region,
                system_prompt=system_prompt,
                user_message=prompt,
                max_tokens=int(max_tokens),
                temperature=float(temperature),
            )
            elapsed = time.monotonic() - t0
            _logger.info(
                "seed QC: Kimi response elapsed=%.2fs in_tokens=%d out_tokens=%d "
                "response_len=%d raw_output=%.500s",
                elapsed,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                len(response_text),
                response_text,
            )

            if _is_degenerate(response_text):
                _logger.warning(
                    "seed QC: degenerate response (%d chars), retrying temp=0.1: %.200s",
                    len(response_text),
                    response_text,
                )
                t0 = time.monotonic()
                response_text, usage = _call_bedrock_converse(
                    api_key=api_key,
                    inference_arn=inference_arn,
                    region=region,
                    system_prompt=system_prompt,
                    user_message=prompt,
                    max_tokens=int(max_tokens),
                    temperature=0.1,
                    top_p=0.7,
                )
                elapsed = time.monotonic() - t0
                _logger.info(
                    "seed QC: Kimi retry response elapsed=%.2fs in_tokens=%d out_tokens=%d "
                    "response_len=%d raw_output=%.500s",
                    elapsed,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    len(response_text),
                    response_text,
                )

        except Exception as e:
            _logger.exception("seed QC: Bedrock call failed")
            return {"error": str(e)[:500]}

        parsed_json = _parse_json_response(response_text)
        qc_verdict = _parse_qc_verdict(response_text)

        result = {
            "success": True,
            "response": response_text,
            "usage": usage,
        }
        if parsed_json is not None:
            result["parsed_json"] = parsed_json
        if qc_verdict is not None:
            result["qc_result"] = qc_verdict

        return result

    @http.route("/talos/trajectory_qc", type="json", auth="user")
    def trajectory_qc(self, record_id=0, field_name="", entry_index=-1, **kw):
        record_id = int(record_id or 0)
        entry_index = int(entry_index if entry_index is not None else -1)
        if not record_id or not field_name:
            return {"error": "record_id and field_name are required"}

        task = request.env["talos.talos"].browse(record_id)
        if not task.exists():
            return {"error": "Task not found"}

        raw = task[field_name] or ""
        if not raw.strip():
            return {"error": "No trajectory data"}

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"error": "Could not parse trajectory JSON"}

        entries = data if isinstance(data, list) else [data]
        if entry_index < 0 or entry_index >= len(entries):
            return {"error": "Invalid entry index"}

        entry = entries[entry_index]
        traj = entry.get("trajectory", entry)
        trajectory_str = json.dumps(traj, ensure_ascii=False)

        entries[entry_index]["qc_status"] = "pending"
        if "qc_result" in entries[entry_index]:
            del entries[entry_index]["qc_result"]
        task.write({field_name: json.dumps(data, indent=2, ensure_ascii=False)})

        db_name = request.env.cr.dbname

        def _submit():
            _QC_POOL.submit(
                _run_trajectory_qc_background,
                db_name,
                record_id,
                field_name,
                entry_index,
                trajectory_str,
            )

        request.env.cr.postcommit.add(_submit)

        return {"success": True}

    @http.route("/talos/generate_task_description", type="json", auth="user")
    def generate_task_description(
        self, record_id=0, field_name="", entry_index=-1, **kw
    ):
        """Trigger background task-description generation for a trajectory entry."""
        record_id = int(record_id or 0)
        entry_index = int(entry_index if entry_index is not None else -1)
        if not record_id or not field_name:
            return {"error": "record_id and field_name are required"}

        task = request.env["talos.talos"].browse(record_id)
        if not task.exists():
            return {"error": "Task not found"}

        raw = task[field_name] or ""
        if not raw.strip():
            return {"error": "No trajectory data"}

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"error": "Could not parse trajectory JSON"}

        entries = data if isinstance(data, list) else [data]
        if entry_index < 0 or entry_index >= len(entries):
            return {"error": "Invalid entry index"}

        entry = entries[entry_index]
        traj = entry.get("trajectory", entry)
        seed_prompt = task.seed_prompt or ""
        messages = traj.get("messages", [])

        entries[entry_index]["task_description_status"] = "pending"
        task.write({field_name: json.dumps(data, indent=2, ensure_ascii=False)})

        db_name = request.env.cr.dbname

        from ..models.talos import _TASKDESC_POOL
        from ..models.talos_sandbox import _inject_task_description_bg

        def _submit():
            _TASKDESC_POOL.submit(
                _inject_task_description_bg,
                db_name,
                record_id,
                field_name,
                seed_prompt,
                messages,
                entry_index,
            )

        request.env.cr.postcommit.add(_submit)

        return {"success": True}

    @http.route("/talos/abort_trajectory_action", type="json", auth="user")
    def abort_trajectory_action(
        self, record_id=0, field_name="", entry_index=-1, action_type="", **kw
    ):
        """Abort a running QC or task-description generation by flipping status to 'aborted'."""
        record_id = int(record_id or 0)
        entry_index = int(entry_index if entry_index is not None else -1)
        if not record_id or not field_name or not action_type:
            return {"error": "record_id, field_name, and action_type are required"}

        status_key = {
            "qc": "qc_status",
            "task_description": "task_description_status",
        }.get(action_type)
        if not status_key:
            return {"error": "action_type must be 'qc' or 'task_description'"}

        task = request.env["talos.talos"].browse(record_id)
        if not task.exists():
            return {"error": "Task not found"}

        raw = task[field_name] or ""
        if not raw.strip():
            return {"error": "No trajectory data"}

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"error": "Could not parse trajectory JSON"}

        entries = data if isinstance(data, list) else [data]
        if entry_index < 0 or entry_index >= len(entries):
            return {"error": "Invalid entry index"}

        current_status = entries[entry_index].get(status_key)
        if current_status != "pending":
            return {"error": "Action is not currently running"}

        entries[entry_index][status_key] = "aborted"
        task.write({field_name: json.dumps(data, indent=2, ensure_ascii=False)})

        return {"success": True}

    @http.route(
        "/api/talos/llm_assist_qc",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def llm_assist_qc_legacy(self, **params):
        try:
            try:
                jdata = request.get_json_data()
            except Exception:
                jdata = {}

            prompt = (jdata.get("prompt") or "").strip()
            if not prompt:
                return request.make_json_response(
                    {"error": "prompt is required", "status": 400}, status=400
                )

            system_prompt = (jdata.get("system_prompt") or "").strip()
            if not system_prompt:
                system_prompt = _get_system_prompt()

            max_tokens = int(jdata.get("max_tokens", 4096))
            temperature = float(jdata.get("temperature", 0.7))

            env = _load_dotenv()
            api_key = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
            if not api_key:
                return request.make_json_response(
                    {
                        "error": "AWS_BEARER_TOKEN_BEDROCK not set in .env",
                        "status": 500,
                    },
                    status=500,
                )

            ICP = request.env["ir.config_parameter"].sudo()
            inference_arn = (ICP.get_param("talos.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("talos.bedrock_region") or "ap-south-1").strip()

            if not inference_arn:
                return request.make_json_response(
                    {"error": "Bedrock Inference ARN not configured", "status": 500},
                    status=500,
                )

            _logger.info(
                "legacy QC: calling Kimi arn=%s region=%s prompt_len=%d",
                inference_arn,
                region,
                len(prompt),
            )
            t0 = time.monotonic()
            response_text, usage = _call_bedrock_converse(
                api_key=api_key,
                inference_arn=inference_arn,
                region=region,
                system_prompt=system_prompt,
                user_message=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            elapsed = time.monotonic() - t0
            _logger.info(
                "legacy QC: Kimi response elapsed=%.2fs in_tokens=%d out_tokens=%d "
                "response_len=%d raw_output=%.500s",
                elapsed,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                len(response_text),
                response_text,
            )

            parsed_json = _parse_json_response(response_text)

            result = {
                "success": True,
                "status": 200,
                "response": response_text,
                "usage": usage,
            }
            if parsed_json is not None:
                result["parsed_json"] = parsed_json

            return request.make_json_response(result)

        except Exception as e:
            _logger.exception("llm_assist_qc error")
            return request.make_json_response(
                {"error": str(e), "status": 500}, status=500
            )
