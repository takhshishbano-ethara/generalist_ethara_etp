import json
import logging
import os
import struct
import time
from urllib.parse import quote

import httpx

from odoo import api, http, SUPERUSER_ID
from odoo.modules.registry import Registry
from odoo.http import request
from werkzeug.wrappers import Response as WerkzeugResponse

_logger = logging.getLogger(__name__)

BEDROCK_CONVERSE_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
)
BEDROCK_CONVERSE_STREAM_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse-stream"
)

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts")

SONNET_MAX_TOKENS = 64000
KIMI_MAX_TOKENS = 16384


def _load_system_prompt():
    path = os.path.join(PROMPTS_DIR, "traj_gen.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        _logger.warning("System prompt file not found: %s", path)
        return ""


def _load_qc_prompt():
    path = os.path.join(PROMPTS_DIR, "qc_review.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        _logger.warning("QC prompt file not found: %s", path)
        return ""


def _load_improve_prompt():
    path = os.path.join(PROMPTS_DIR, "improve_trajectory.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        _logger.warning("Improve prompt file not found: %s", path)
        return ""


def _get_golden_content(task):
    if task.golden_input_mode == "manual":
        return (task.golden_trajectory_manual or "").strip()
    return (task.golden_trajectory or "").strip()


def _parse_spawned_agents(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _build_user_message(task, scenario_text):
    parts = []

    if task:
        parts.append("## TASK INFORMATION")
        parts.append("- Task ID: %s" % (task.task_id or ""))
        if task.persona_id:
            parts.append("- Persona: %s" % task.persona_id.name)
        if task.heart_taxonomy:
            parts.append("- HEART Taxonomy: %s" % ", ".join(task.heart_taxonomy.mapped("name")))
        if task.task_type:
            parts.append("- Task Type: %s" % task.task_type)
        if task.difficulty:
            parts.append("- Difficulty: %s" % task.difficulty)
        if task.trajectory_modifier:
            parts.append("- Trajectory Modifier: %s" % task.trajectory_modifier)
        if task.safety_critical:
            parts.append("- Safety Critical: %s" % task.safety_critical)
        parts.append("")

        prompt_text = (scenario_text or "").strip() or (task.seed_prompt or "").strip()
        if prompt_text:
            parts.append("## THE USER'S SINGLE COMPLEX PROMPT")
            parts.append("(This is the ONLY user message. The orchestrator must decompose it into parallel sub-agent tasks.)\n")
            parts.append(prompt_text)
            parts.append("")

        spawned_agents = _parse_spawned_agents(task.spawned_agents)
        if spawned_agents:
            parts.append("## SPAWNED AGENTS (the orchestrator MUST spawn exactly these)")
            for i, agent in enumerate(spawned_agents, 1):
                parts.append("  %d. %s: %s" % (i, agent.get("name", "agent_%d" % i), agent.get("role", "")))
            parts.append("")

        parts.append("## PERSONA DATA\n")
        if task.agent_md and task.agent_md.strip():
            parts.append("### AGENTS.md\n%s\n" % task.agent_md.strip())
        if task.soul_md and task.soul_md.strip():
            parts.append("### SOUL.md\n%s\n" % task.soul_md.strip())
        if task.memory_md and task.memory_md.strip():
            parts.append("### MEMORY.md\n%s\n" % task.memory_md.strip())
    else:
        prompt_text = (scenario_text or "").strip() or "Generate a diverse, high-quality golden trajectory."
        parts.append("## THE USER'S SINGLE COMPLEX PROMPT\n")
        parts.append(prompt_text)
        parts.append("")

    num_agents = len(_parse_spawned_agents(task.spawned_agents)) if task and task.spawned_agents else 2
    parts.append("## GENERATION INSTRUCTIONS\n")
    parts.append("Generate a complete multi-agent golden trajectory following ALL the schema rules from the system prompt.")
    parts.append("")
    parts.append("IMPORTANT CONSTRAINTS:")
    parts.append("- Each sub-agent should make at least 3-5 tool calls to demonstrate real work")
    parts.append("- Tool results must be plausible and realistic")
    if task and task.spawned_agents:
        parts.append("- The orchestrator MUST spawn exactly %d agents matching the metadata above" % num_agents)
    parts.append("- The sessions_spawn task descriptions must be detailed (3+ paragraphs)")
    parts.append("- Set system_prompt to empty string \"\"")
    parts.append("- Set platform to \"macOS\"")
    parts.append("- Output ONLY the raw JSON object. No markdown fences, no explanation, no commentary.")

    return "\n".join(parts)


def _build_qc_message(task, content):
    parts = ["## Trajectory Under Review\n```json", content.strip(), "```\n"]

    parts.append("## Task Input Data\n")
    if task.persona_id:
        parts.append("- Persona: %s" % task.persona_id.name)
    if task.heart_taxonomy:
        parts.append("- HEART Taxonomy: %s" % ", ".join(task.heart_taxonomy.mapped("name")))
    if task.task_type:
        parts.append("- Task Type: %s" % task.task_type)
    if task.difficulty:
        parts.append("- Difficulty: %s" % task.difficulty)

    spawned_agents = _parse_spawned_agents(task.spawned_agents)
    if spawned_agents:
        parts.append("\n### Expected Spawned Agents")
        for i, agent in enumerate(spawned_agents, 1):
            parts.append("  %d. %s: %s" % (i, agent.get("name", ""), agent.get("role", "")))

    if task.seed_prompt:
        parts.append("\n### Seed Prompt\n%s" % task.seed_prompt.strip())
    if task.soul_md:
        parts.append("\n### Soul MD\n%s" % task.soul_md.strip())

    return "\n".join(parts)


def _build_improve_message(task, trajectory, qc_result, structural_result):
    parts = ["## Current Trajectory\n```json", trajectory.strip(), "```\n"]

    parts.append("## QC Feedback\n```json")
    parts.append(qc_result.strip())
    parts.append("```\n")

    if structural_result and structural_result.strip():
        parts.append("## Structural Validation\n```")
        parts.append(structural_result.strip())
        parts.append("```\n")

    parts.append("## Task Input Data\n")
    if task.persona_id:
        parts.append("- Persona: %s" % task.persona_id.name)
    if task.heart_taxonomy:
        parts.append("- HEART Taxonomy: %s" % ", ".join(task.heart_taxonomy.mapped("name")))
    if task.task_type:
        parts.append("- Task Type: %s" % task.task_type)

    spawned_agents = _parse_spawned_agents(task.spawned_agents)
    if spawned_agents:
        parts.append("\n### Expected Spawned Agents")
        for i, agent in enumerate(spawned_agents, 1):
            parts.append("  %d. %s: %s" % (i, agent.get("name", ""), agent.get("role", "")))

    if task.seed_prompt:
        parts.append("\n### Seed Prompt\n%s" % task.seed_prompt.strip())
    if task.soul_md:
        parts.append("\n### Soul MD\n%s" % task.soul_md.strip())

    parts.append("\n## Instructions\nFix the issues identified in the QC feedback. Return the complete improved trajectory JSON.")

    return "\n".join(parts)


def _call_bedrock_converse(
    api_key,
    inference_arn,
    region,
    system_prompt,
    user_message,
    max_tokens=SONNET_MAX_TOKENS,
    temperature=0.7,
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
            {"role": "user", "content": [{"text": user_message}]},
        ],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    }
    if system_prompt:
        payload["system"] = [{"text": system_prompt}]

    with httpx.Client(http2=True, timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        error_detail = resp.text[:500]
        raise RuntimeError(
            "Bedrock API error (HTTP %d): %s" % (resp.status_code, error_detail)
        )

    result = resp.json()
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


def _parse_event_headers(data):
    headers = {}
    pos = 0
    end = len(data)
    while pos < end:
        if pos + 1 > end:
            break
        name_len = data[pos]
        pos += 1
        if pos + name_len > end:
            break
        name = data[pos: pos + name_len].decode("utf-8")
        pos += name_len
        if pos + 1 > end:
            break
        vtype = data[pos]
        pos += 1
        if vtype == 7:
            if pos + 2 > end:
                break
            vlen = struct.unpack(">H", data[pos: pos + 2])[0]
            pos += 2
            if pos + vlen > end:
                break
            headers[name] = data[pos: pos + vlen].decode("utf-8")
            pos += vlen
        elif vtype in (0, 1):
            headers[name] = vtype == 0
        elif vtype == 2:
            pos += 1
        elif vtype == 3:
            pos += 2
        elif vtype == 4:
            if pos + 2 > end:
                break
            vlen = struct.unpack(">H", data[pos: pos + 2])[0]
            pos += 2 + vlen
        elif vtype == 5:
            pos += 4
        elif vtype == 6:
            pos += 8
        elif vtype == 8:
            pos += 8
        elif vtype == 9:
            pos += 16
        else:
            break
    return headers


def _iter_event_stream(raw_iter):
    buf = b""
    for chunk in raw_iter:
        buf += chunk
        while len(buf) >= 12:
            total_len = struct.unpack(">I", buf[0:4])[0]
            if total_len < 16 or len(buf) < total_len:
                break
            headers_len = struct.unpack(">I", buf[4:8])[0]
            headers = _parse_event_headers(buf[12: 12 + headers_len])
            payload_start = 12 + headers_len
            payload_end = total_len - 4
            payload_bytes = buf[payload_start:payload_end]

            event_type = headers.get(":event-type", "")
            payload = {}
            if payload_bytes:
                try:
                    payload = json.loads(payload_bytes.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

            yield event_type, payload
            buf = buf[total_len:]


def _sse_line(data):
    return ("data: %s\n\n" % json.dumps(data, ensure_ascii=False)).encode("utf-8")


def _stream_bedrock_sse(
    api_key,
    inference_arn,
    region,
    system_prompt,
    user_message,
    max_tokens=SONNET_MAX_TOKENS,
    temperature=0.7,
    timeout=600.0,
):
    url = BEDROCK_CONVERSE_STREAM_URL.format(
        region=region,
        model_id=quote(inference_arn, safe=""),
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % api_key,
    }
    payload = {
        "messages": [
            {"role": "user", "content": [{"text": user_message}]},
        ],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    }
    if system_prompt:
        payload["system"] = [{"text": system_prompt}]

    try:
        with httpx.Client(http2=False, timeout=timeout) as client:
            with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    err_bytes = b""
                    for chunk in resp.iter_raw():
                        err_bytes += chunk
                        if len(err_bytes) > 2000:
                            break
                    yield _sse_line({
                        "type": "error",
                        "message": "Bedrock HTTP %d: %s" % (
                            resp.status_code,
                            err_bytes.decode("utf-8", errors="replace")[:500],
                        ),
                    })
                    return

                for event_type, data in _iter_event_stream(resp.iter_raw()):
                    if event_type == "contentBlockDelta":
                        text = data.get("delta", {}).get("text", "")
                        if text:
                            yield _sse_line({"type": "delta", "text": text})
                    elif event_type == "messageStop":
                        yield _sse_line({
                            "type": "stop",
                            "stopReason": data.get("stopReason", "end_turn"),
                        })
                    elif event_type == "metadata":
                        usage = data.get("usage", {})
                        yield _sse_line({
                            "type": "metadata",
                            "usage": {
                                "input_tokens": usage.get("inputTokens", 0),
                                "output_tokens": usage.get("outputTokens", 0),
                            },
                        })

        yield _sse_line({"type": "done"})
    except Exception as e:
        _logger.exception("Bedrock stream error")
        yield _sse_line({"type": "error", "message": str(e)})


def _log_generation(db_name, uid, task_id, inference_arn, usage, duration,
                    status="success", error=None, call_type="generate"):
    try:
        reg = Registry(db_name)
        with reg.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["skoll.generation"].create({
                "task_id": int(task_id) if task_id else False,
                "user_id": uid,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "model_arn": inference_arn or "",
                "duration_s": duration,
                "status": status,
                "error_message": error,
                "call_type": call_type,
            })
    except Exception:
        _logger.exception("Failed to log generation")


def _update_qc_result(db_name, task_db_id, accumulated, status):
    try:
        reg = Registry(db_name)
        with reg.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["skoll.skoll"].browse(int(task_db_id))
            if not task.exists():
                return
            vals = {"golden_qc_result": accumulated}
            if status == "error":
                vals["golden_qc_status"] = "error"
            else:
                verdict = "error"
                cleaned = accumulated.strip()
                if cleaned.startswith("```"):
                    first_nl = cleaned.find("\n")
                    if first_nl != -1:
                        cleaned = cleaned[first_nl + 1:]
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3].rstrip()
                for attempt in (cleaned, None):
                    if attempt is None:
                        first_b = cleaned.find("{")
                        last_b = cleaned.rfind("}")
                        if first_b == -1 or last_b <= first_b:
                            break
                        attempt = cleaned[first_b:last_b + 1]
                    try:
                        parsed = json.loads(attempt)
                        v = (parsed.get("verdict") or "").lower()
                        if v in ("pass", "fail", "needs_revision"):
                            verdict = v
                            break
                    except (json.JSONDecodeError, AttributeError):
                        continue
                vals["golden_qc_status"] = verdict
            task.write(vals)
    except Exception:
        _logger.exception("Failed to update QC result")


def _update_content_after_improve(db_name, task_db_id, accumulated):
    try:
        reg = Registry(db_name)
        with reg.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["skoll.skoll"].browse(int(task_db_id))
            if task.exists():
                field = "golden_trajectory"
                if task.golden_input_mode == "manual":
                    field = "golden_trajectory_manual"
                task.write({
                    field: accumulated,
                    "golden_qc_status": "pending",
                    "golden_qc_result": False,
                    "golden_structural_result": False,
                })
    except Exception:
        _logger.exception("Failed to update content after improve")


class SkollGoldenGenerationController(http.Controller):

    @http.route(
        "/skoll/golden/generate_stream",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def golden_generate_stream(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        inference_arn = ICP.get_param("skoll.bedrock_inference_arn", "")
        region = ICP.get_param("skoll.bedrock_region", "ap-south-1")
        api_key = os.environ.get("SKOLL_BEDROCK_API_KEY", "")

        try:
            body = json.loads(request.httprequest.data or b"{}")
        except (json.JSONDecodeError, TypeError):
            body = {}

        prompt = body.get("prompt", "")
        record_id = body.get("record_id")
        max_tokens = int(body.get("max_tokens", SONNET_MAX_TOKENS))
        temperature = float(body.get("temperature", 0.2))

        sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        if not inference_arn or not api_key:
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "Bedrock not configured. Set Inference ARN in settings and SKOLL_BEDROCK_API_KEY env var."}),
                mimetype="text/event-stream",
                headers=sse_headers,
            )

        task = None
        if record_id:
            task = request.env["skoll.skoll"].sudo().browse(int(record_id))
            if not task.exists():
                task = None

        system_prompt = _load_system_prompt()
        user_message = _build_user_message(task, prompt)

        db_name = request.env.cr.dbname
        uid = request.env.uid

        def _logged_stream():
            t0 = time.time()
            usage = {}
            status = "success"
            error = None
            try:
                for chunk in _stream_bedrock_sse(
                    api_key=api_key,
                    inference_arn=inference_arn,
                    region=region,
                    system_prompt=system_prompt,
                    user_message=user_message,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ):
                    yield chunk
                    try:
                        line = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                        payload = json.loads(line.split("data: ", 1)[1].split("\n")[0])
                        if payload.get("type") == "metadata":
                            usage = payload.get("usage", {})
                        elif payload.get("type") == "error":
                            status = "error"
                            error = payload.get("message", "")[:500]
                    except (IndexError, json.JSONDecodeError, KeyError):
                        pass
            except Exception as e:
                status = "error"
                error = str(e)[:500]
            finally:
                duration = round(time.time() - t0, 2)
                _log_generation(db_name, uid, record_id, inference_arn, usage, duration, status, error)

        return WerkzeugResponse(
            _logged_stream(),
            mimetype="text/event-stream",
            headers=sse_headers,
            direct_passthrough=True,
        )

    @http.route(
        "/skoll/golden/qc_stream",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def golden_qc_stream(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        qc_arn = ICP.get_param("skoll.qc_inference_arn", "")
        region = ICP.get_param("skoll.bedrock_region", "ap-south-1")
        api_key = os.environ.get("SKOLL_BEDROCK_API_KEY", "")

        try:
            body = json.loads(request.httprequest.data or b"{}")
        except (json.JSONDecodeError, TypeError):
            body = {}

        record_id = body.get("record_id")

        sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        if not qc_arn or not api_key:
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "QC not configured. Set QC ARN in settings and SKOLL_BEDROCK_API_KEY env var."}),
                mimetype="text/event-stream", headers=sse_headers,
            )

        if not record_id:
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "record_id is required."}),
                mimetype="text/event-stream", headers=sse_headers,
            )

        task = request.env["skoll.skoll"].sudo().browse(int(record_id))
        if not task.exists():
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "Task not found."}),
                mimetype="text/event-stream", headers=sse_headers,
            )

        content = _get_golden_content(task)
        if not content:
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "No golden trajectory content to QC."}),
                mimetype="text/event-stream", headers=sse_headers,
            )

        task.write({"golden_qc_status": "running"})

        from odoo.addons.skoll.models.golden_trajectory_validator import validate_trajectory, format_result
        structural = validate_trajectory(content, task_data={
            "spawned_agents": task.spawned_agents or "",
        })
        structural_text = format_result(structural)
        task.write({"golden_structural_result": structural_text})

        qc_prompt = _load_qc_prompt()
        user_msg = _build_qc_message(task, content)
        if not structural["valid"]:
            user_msg += "\n\n## Structural Validation (pre-computed)\n%s" % structural_text

        db_name = request.env.cr.dbname
        uid = request.env.uid
        task_db_id = task.id

        def _qc_logged_stream():
            t0 = time.time()
            usage = {}
            status = "success"
            error = None
            accumulated = ""
            try:
                for chunk in _stream_bedrock_sse(
                    api_key=api_key,
                    inference_arn=qc_arn,
                    region=region,
                    system_prompt=qc_prompt,
                    user_message=user_msg,
                    max_tokens=KIMI_MAX_TOKENS,
                    temperature=0.7,
                ):
                    yield chunk
                    try:
                        line = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                        payload = json.loads(line.split("data: ", 1)[1].split("\n")[0])
                        if payload.get("type") == "delta" and payload.get("text"):
                            accumulated += payload["text"]
                        elif payload.get("type") == "metadata":
                            usage = payload.get("usage", {})
                        elif payload.get("type") == "error":
                            status = "error"
                            error = payload.get("message", "")[:500]
                    except (IndexError, json.JSONDecodeError, KeyError):
                        pass
            except Exception as e:
                status = "error"
                error = str(e)[:500]
            finally:
                duration = round(time.time() - t0, 2)
                _log_generation(
                    db_name, uid, task_db_id, qc_arn, usage, duration,
                    status, error, call_type="qc",
                )
                _update_qc_result(db_name, task_db_id, accumulated, status)

        return WerkzeugResponse(
            _qc_logged_stream(),
            mimetype="text/event-stream",
            headers=sse_headers,
            direct_passthrough=True,
        )

    @http.route(
        "/skoll/golden/improve_stream",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def golden_improve_stream(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        inference_arn = ICP.get_param("skoll.bedrock_inference_arn", "")
        region = ICP.get_param("skoll.bedrock_region", "ap-south-1")
        api_key = os.environ.get("SKOLL_BEDROCK_API_KEY", "")

        try:
            body = json.loads(request.httprequest.data or b"{}")
        except (json.JSONDecodeError, TypeError):
            body = {}

        record_id = body.get("record_id")

        sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        if not inference_arn or not api_key:
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "Bedrock not configured."}),
                mimetype="text/event-stream", headers=sse_headers,
            )

        if not record_id:
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "record_id is required."}),
                mimetype="text/event-stream", headers=sse_headers,
            )

        task = request.env["skoll.skoll"].sudo().browse(int(record_id))
        if not task.exists():
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "Task not found."}),
                mimetype="text/event-stream", headers=sse_headers,
            )

        trajectory = _get_golden_content(task)
        qc_result = (task.golden_qc_result or "").strip()
        if not trajectory or not qc_result:
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "Both trajectory and QC result are required for improvement."}),
                mimetype="text/event-stream", headers=sse_headers,
            )

        improve_prompt = _load_improve_prompt()
        user_msg = _build_improve_message(
            task, trajectory, qc_result,
            task.golden_structural_result or "",
        )

        db_name = request.env.cr.dbname
        uid = request.env.uid
        task_db_id = task.id

        def _improve_logged_stream():
            t0 = time.time()
            usage = {}
            status = "success"
            error = None
            accumulated = ""
            try:
                for chunk in _stream_bedrock_sse(
                    api_key=api_key,
                    inference_arn=inference_arn,
                    region=region,
                    system_prompt=improve_prompt,
                    user_message=user_msg,
                    max_tokens=SONNET_MAX_TOKENS,
                    temperature=0.2,
                ):
                    yield chunk
                    try:
                        line = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                        payload = json.loads(line.split("data: ", 1)[1].split("\n")[0])
                        if payload.get("type") == "delta" and payload.get("text"):
                            accumulated += payload["text"]
                        elif payload.get("type") == "metadata":
                            usage = payload.get("usage", {})
                        elif payload.get("type") == "error":
                            status = "error"
                            error = payload.get("message", "")[:500]
                    except (IndexError, json.JSONDecodeError, KeyError):
                        pass
            except Exception as e:
                status = "error"
                error = str(e)[:500]
            finally:
                duration = round(time.time() - t0, 2)
                _log_generation(
                    db_name, uid, task_db_id, inference_arn, usage, duration,
                    status, error, call_type="improve",
                )
                if accumulated.strip() and status == "success":
                    _update_content_after_improve(db_name, task_db_id, accumulated)

        return WerkzeugResponse(
            _improve_logged_stream(),
            mimetype="text/event-stream",
            headers=sse_headers,
            direct_passthrough=True,
        )

    @http.route("/skoll/golden/spawn_tree", type="json", auth="user", methods=["POST"])
    def golden_spawn_tree(self, record_id=None, **kw):
        from odoo.addons.skoll.models.spawn_tree_util import build_spawn_tree

        if not record_id:
            return {"status": "error", "message": "record_id is required."}

        task = request.env["skoll.skoll"].sudo().browse(int(record_id))
        if not task.exists():
            return {"status": "error", "message": "Task not found."}

        content = _get_golden_content(task)
        if not content:
            return {"status": "error", "message": "No trajectory content to build spawn tree."}

        tree_text = build_spawn_tree(content)
        task.write({"golden_spawn_tree": tree_text})

        return {"status": "success", "spawn_tree": tree_text}
