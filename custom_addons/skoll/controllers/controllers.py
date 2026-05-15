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

# ── Max output token ceilings (per official docs) ──────────────────────
# Claude Sonnet 4 / 4.5 / 4.6: 64 000  (Anthropic Models Overview)
# Kimi K2.5:                    16 384  (AWS Bedrock Model Card – "16K")
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


def _build_user_message(record_id, scenario_text):
    parts = []

    task = None
    if record_id:
        try:
            task = request.env["skoll.skoll"].sudo().browse(int(record_id))
            if not task.exists():
                task = None
        except Exception:
            _logger.warning("Could not load task data for record %s", record_id)

    if task:
        parts.append("## TASK INFORMATION")
        parts.append("- Task ID: %s" % (task.task_id or ""))
        if task.persona_id:
            parts.append("- Persona: %s" % task.persona_id.name)
        if task.life_domain_ids:
            parts.append("- Life Domain: %s" % ", ".join(task.life_domain_ids.mapped("name")))
        if task.cluster_ids:
            parts.append("- Cluster: %s" % ", ".join(task.cluster_ids.mapped("name")))
        if task.task_type_ids:
            parts.append("- Task Type: %s" % ", ".join(task.task_type_ids.mapped("name")))
        if task.pattern_taxonomy_ids:
            parts.append("- Pattern Taxonomy: %s" % ", ".join(task.pattern_taxonomy_ids.mapped("name")))
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
    parts.append("- Set system_prompt to \"PLACEHOLDER_SYSTEM_PROMPT\"")
    parts.append("- Output ONLY the raw JSON object. No markdown fences, no explanation, no commentary.")

    return "\n".join(parts)


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


def _parse_jsonl(text):
    data = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data.append(json.loads(line))
        except json.JSONDecodeError:
            _logger.warning("Skipping malformed JSONL line")
            continue
    return data


def _read_jsonl_source(jdata):
    """Read JSONL content from local path or remote URL.

    Returns (text, error).  On success error is None.
    """
    file_path = (jdata.get("path") or "").strip()
    url = (jdata.get("url") or "").strip()

    if file_path:
        expanded = os.path.expanduser(file_path)
        if not os.path.isfile(expanded):
            return None, "File not found: %s" % file_path
        with open(expanded, "r", encoding="utf-8") as fh:
            return fh.read(), None

    if url:
        resp = httpx.get(url, timeout=60)
        resp.raise_for_status()
        return resp.text, None

    return None, "Provide 'path' (local file) or 'url' (remote)."


def _json_response(payload, status=200):
    return http.Response(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        content_type="application/json; charset=utf-8",
        status=status,
    )


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
            {
                "role": "user",
                "content": [{"text": user_message}],
            },
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
        name = data[pos : pos + name_len].decode("utf-8")
        pos += name_len
        if pos + 1 > end:
            break
        vtype = data[pos]
        pos += 1
        if vtype == 7:  # String
            if pos + 2 > end:
                break
            vlen = struct.unpack(">H", data[pos : pos + 2])[0]
            pos += 2
            if pos + vlen > end:
                break
            headers[name] = data[pos : pos + vlen].decode("utf-8")
            pos += vlen
        elif vtype in (0, 1):  # Bool true / false
            headers[name] = vtype == 0
        elif vtype == 2:  # Byte
            pos += 1
        elif vtype == 3:  # Short
            pos += 2
        elif vtype == 4:  # Bytes
            if pos + 2 > end:
                break
            vlen = struct.unpack(">H", data[pos : pos + 2])[0]
            pos += 2 + vlen
        elif vtype == 5:  # Int
            pos += 4
        elif vtype == 6:  # Long
            pos += 8
        elif vtype == 8:  # Timestamp
            pos += 8
        elif vtype == 9:  # UUID
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
                break  # need more data
            headers_len = struct.unpack(">I", buf[4:8])[0]
            # bytes 8..12 = prelude CRC (skip)
            headers = _parse_event_headers(buf[12 : 12 + headers_len])
            payload_start = 12 + headers_len
            payload_end = total_len - 4  # last 4 bytes = message CRC
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
                    yield _sse_line(
                        {
                            "type": "error",
                            "message": "Bedrock HTTP %d: %s"
                            % (
                                resp.status_code,
                                err_bytes.decode("utf-8", errors="replace")[:500],
                            ),
                        }
                    )
                    return

                for event_type, data in _iter_event_stream(resp.iter_raw()):
                    if event_type == "contentBlockDelta":
                        text = data.get("delta", {}).get("text", "")
                        if text:
                            yield _sse_line({"type": "delta", "text": text})
                    elif event_type == "messageStop":
                        yield _sse_line(
                            {
                                "type": "stop",
                                "stopReason": data.get("stopReason", "end_turn"),
                            }
                        )
                    elif event_type == "metadata":
                        usage = data.get("usage", {})
                        yield _sse_line(
                            {
                                "type": "metadata",
                                "usage": {
                                    "input_tokens": usage.get("inputTokens", 0),
                                    "output_tokens": usage.get("outputTokens", 0),
                                },
                            }
                        )
                    # messageStart / contentBlockStart / contentBlockStop → skip

        yield _sse_line({"type": "done"})
    except Exception as e:
        _logger.exception("Bedrock stream error")
        yield _sse_line({"type": "error", "message": str(e)})


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
    if task.life_domain_ids:
        parts.append("- Life Domain: %s" % ", ".join(task.life_domain_ids.mapped("name")))
    if task.cluster_ids:
        parts.append("- Cluster: %s" % ", ".join(task.cluster_ids.mapped("name")))
    if task.task_type_ids:
        parts.append("- Task Type: %s" % ", ".join(task.task_type_ids.mapped("name")))
    if task.pattern_taxonomy_ids:
        parts.append("- Pattern Taxonomy: %s" % ", ".join(task.pattern_taxonomy_ids.mapped("name")))

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


def _build_qc_message(task, content):
    parts = ["## Trajectory Under Review\n```json", content.strip(), "```\n"]

    parts.append("## Task Input Data\n")
    if task.persona_id:
        parts.append("- Persona: %s" % task.persona_id.name)
    if task.life_domain_ids:
        parts.append("- Life Domain: %s" % ", ".join(task.life_domain_ids.mapped("name")))
    if task.cluster_ids:
        parts.append("- Cluster: %s" % ", ".join(task.cluster_ids.mapped("name")))
    if task.task_type_ids:
        parts.append("- Task Type: %s" % ", ".join(task.task_type_ids.mapped("name")))
    if task.pattern_taxonomy_ids:
        parts.append("- Pattern Taxonomy: %s" % ", ".join(task.pattern_taxonomy_ids.mapped("name")))

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
            vals = {"qc_result": accumulated}
            if status == "error":
                vals["qc_status"] = "error"
            else:
                verdict = "error"
                try:
                    parsed = json.loads(accumulated.strip())
                    v = (parsed.get("verdict") or "").lower()
                    if v in ("pass", "fail", "needs_revision"):
                        verdict = v
                except (json.JSONDecodeError, AttributeError):
                    pass
                vals["qc_status"] = verdict
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
                task.write({
                    "content": accumulated,
                    "qc_status": "pending",
                    "qc_result": False,
                    "qc_structural_result": False,
                })
    except Exception:
        _logger.exception("Failed to update content after improve")


class SkollController(http.Controller):

    @http.route("/skoll/generate", type="json", auth="user", methods=["POST"])
    def generate_content(
        self, prompt="", record_id=None, max_tokens=SONNET_MAX_TOKENS, temperature=0.2, **kw
    ):
        ICP = request.env["ir.config_parameter"].sudo()

        inference_arn = ICP.get_param("skoll.bedrock_inference_arn", "")
        region = ICP.get_param("skoll.bedrock_region", "ap-south-1")
        api_key = os.environ.get("SKOLL_BEDROCK_API_KEY", "")

        if not inference_arn or not api_key:
            return {
                "status": "error",
                "message": "Bedrock not configured. Set Inference ARN in settings and SKOLL_BEDROCK_API_KEY env var.",
            }

        system_prompt = _load_system_prompt()
        user_message = _build_user_message(record_id, prompt)

        t0 = time.time()
        try:
            response_text, usage = _call_bedrock_converse(
                api_key=api_key,
                inference_arn=inference_arn,
                region=region,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            duration = time.time() - t0
            request.env["skoll.generation"].sudo().create({
                "task_id": int(record_id) if record_id else False,
                "user_id": request.env.uid,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "model_arn": inference_arn,
                "duration_s": round(duration, 2),
                "status": "success",
            })
            return {
                "status": "success",
                "content": response_text,
                "usage": usage,
            }
        except Exception as e:
            duration = time.time() - t0
            try:
                request.env["skoll.generation"].sudo().create({
                    "task_id": int(record_id) if record_id else False,
                    "user_id": request.env.uid,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "model_arn": inference_arn,
                    "duration_s": round(duration, 2),
                    "status": "error",
                    "error_message": str(e)[:500],
                })
            except Exception:
                _logger.exception("Failed to log generation error")
            _logger.exception("Skoll generate failed")
            return {
                "status": "error",
                "message": str(e),
            }

    @http.route(
        "/skoll/generate_stream",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def generate_stream(self, **kw):
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

        sse_headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }

        if not inference_arn or not api_key:
            return WerkzeugResponse(
                _sse_line(
                    {
                        "type": "error",
                        "message": "Bedrock not configured. Set Inference ARN in settings and SKOLL_BEDROCK_API_KEY env var.",
                    }
                ),
                mimetype="text/event-stream",
                headers=sse_headers,
            )

        system_prompt = _load_system_prompt()
        user_message = _build_user_message(record_id, prompt)

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
        "/skoll/qc_stream",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def qc_stream(self, **kw):
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
        if not task.exists() or not (task.content or "").strip():
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "No golden trajectory content to QC."}),
                mimetype="text/event-stream", headers=sse_headers,
            )

        task.write({"qc_status": "running"})

        from odoo.addons.skoll.models.trajectory_validator import validate_trajectory, format_result
        structural = validate_trajectory(task.content, task_data={
            "spawned_agents": task.spawned_agents or "",
        })
        structural_text = format_result(structural)
        task.write({"qc_structural_result": structural_text})

        qc_prompt = _load_qc_prompt()
        user_msg = _build_qc_message(task, task.content)
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
        "/skoll/improve_stream",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def improve_stream(self, **kw):
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

        trajectory = (task.content or "").strip()
        qc_result = (task.qc_result or "").strip()
        if not trajectory or not qc_result:
            return WerkzeugResponse(
                _sse_line({"type": "error", "message": "Both trajectory and QC result are required for improvement."}),
                mimetype="text/event-stream", headers=sse_headers,
            )

        improve_prompt = _load_improve_prompt()
        user_msg = _build_improve_message(
            task, trajectory, qc_result,
            task.qc_structural_result or "",
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

    @http.route(
        "/api/skoll/upload_tasks",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def upload_tasks(self, **kw):
        try:
            try:
                jdata = json.loads(request.httprequest.stream.read())
            except Exception:
                try:
                    jdata = json.loads(request.httprequest.data)
                except Exception:
                    jdata = {}

            text, err = _read_jsonl_source(jdata)
            if err:
                return _json_response({"message": err, "status": 400}, 400)

            data = _parse_jsonl(text)
            if not data:
                return _json_response(
                    {"message": "No valid JSONL lines found", "status": 400}, 400
                )

            TaskModel = request.env["skoll.skoll"].sudo()
            PersonaModel = request.env["skoll.persona"].sudo()

            created_ids = []
            skipped = 0
            required = ("task_id", "persona", "soul.md", "memory.md", "agent.md", "seed_prompt")
            errors = []
            for idx, item in enumerate(data):
                missing = [f for f in required if not (item.get(f) or "").strip()]
                if missing:
                    errors.append("Line %d: missing %s" % (idx + 1, ", ".join(missing)))
                    skipped += 1
                    continue

                tid = item["task_id"].strip()

                if TaskModel.search([("task_id", "=", tid)], limit=1):
                    skipped += 1
                    continue

                persona_name = item["persona"].strip()
                normalized = persona_name.lower().replace(" ", "-")
                persona = PersonaModel.search(
                    [("name", "=", normalized)], limit=1
                )
                if not persona:
                    persona = PersonaModel.create({
                        "name": persona_name,
                        "soul_md": item["soul.md"].strip(),
                        "memory_md": item["memory.md"].strip(),
                        "agents_md": item["agent.md"].strip(),
                    })

                vals = {
                    "task_id": tid,
                    "persona_id": persona.id,
                    "seed_prompt": item["seed_prompt"].strip(),
                    "soul_md": item["soul.md"].strip(),
                    "memory_md": item["memory.md"].strip(),
                    "agent_md": item["agent.md"].strip(),
                }
                TAG_FIELDS = (
                    ("life_domain", "life_domain_ids", "skoll.tag.life_domain"),
                    ("cluster", "cluster_ids", "skoll.tag.cluster"),
                    ("task_type", "task_type_ids", "skoll.tag.task_type"),
                    ("pattern_taxonomy", "pattern_taxonomy_ids", "skoll.tag.pattern_taxonomy"),
                )
                for jsonl_key, model_field, tag_model in TAG_FIELDS:
                    raw = (item.get(jsonl_key) or "").strip()
                    if not raw:
                        continue
                    TagModel = request.env[tag_model].sudo()
                    tag_ids = []
                    for part in raw.split(","):
                        tag_name = part.strip()
                        if not tag_name:
                            continue
                        tag = TagModel.search([("name", "=ilike", tag_name)], limit=1)
                        if not tag:
                            tag = TagModel.create({"name": tag_name})
                        tag_ids.append(tag.id)
                    if tag_ids:
                        vals[model_field] = [(6, 0, tag_ids)]

                for plain_key in ("credential", "password", "prerequisites"):
                    v = (item.get(plain_key) or "").strip()
                    if v:
                        vals[plain_key] = v

                sa = item.get("spawned_agents")
                if sa is not None:
                    if isinstance(sa, list):
                        vals["spawned_agents"] = json.dumps(sa, ensure_ascii=False)
                    elif isinstance(sa, str) and sa.strip():
                        vals["spawned_agents"] = sa.strip()
                record = TaskModel.create(vals)
                created_ids.append(record.id)

            result = {
                "success": True,
                "created": len(created_ids),
                "skipped": skipped,
                "status": 200,
            }
            if errors:
                result["errors"] = errors
            return _json_response(result)
        except Exception as e:
            _logger.exception("Skoll task upload failed")
            return _json_response({"error": str(e), "status": 500}, 500)

    @http.route(
        "/api/skoll/upload_personas",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def upload_personas(self, **kw):
        try:
            try:
                jdata = json.loads(request.httprequest.stream.read())
            except Exception:
                try:
                    jdata = json.loads(request.httprequest.data)
                except Exception:
                    jdata = {}

            text, err = _read_jsonl_source(jdata)
            if err:
                return _json_response({"message": err, "status": 400}, 400)

            data = _parse_jsonl(text)
            if not data:
                return _json_response(
                    {"message": "No valid JSONL lines found", "status": 400}, 400
                )

            PersonaModel = request.env["skoll.persona"].sudo()

            created = 0
            updated = 0
            for item in data:
                name = (item.get("name") or "").strip()
                if not name:
                    continue

                normalized = name.lower().replace(" ", "-")
                existing = PersonaModel.search(
                    [("name", "=", normalized)], limit=1
                )

                vals = {}
                if item.get("soul_md"):
                    vals["soul_md"] = item["soul_md"]
                if item.get("memory_md"):
                    vals["memory_md"] = item["memory_md"]
                if item.get("agents_md"):
                    vals["agents_md"] = item["agents_md"]

                if existing:
                    if vals:
                        existing.write(vals)
                    updated += 1
                else:
                    vals["name"] = name
                    PersonaModel.create(vals)
                    created += 1

            return _json_response({
                "success": True,
                "created": created,
                "updated": updated,
                "status": 200,
            })
        except Exception as e:
            _logger.exception("Skoll persona upload failed")
            return _json_response({"error": str(e), "status": 500}, 500)
