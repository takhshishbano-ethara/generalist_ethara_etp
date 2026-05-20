# -*- coding: utf-8 -*-
import base64
import json
import logging
import mimetypes
import os
import threading
import uuid

from odoo import http
from odoo.http import request

from ..models.kensei_sandbox import MODEL_DEFAULTS, TRAJECTORY_FIELD_MAP

_logger = logging.getLogger(__name__)

def _upload_to_s3_background(bucket, region, prefix, task_id, files_meta, subfolder="input", access_key=None, secret_key=None):
    try:
        import boto3
        from botocore.config import Config as BotoConfig

        client_kwargs = {
            "region_name": region,
            "config": BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
        }
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key

        s3 = boto3.client("s3", **client_kwargs)
        for fm in files_meta:
            key = "%s/%s/tasks/%s/%s" % (prefix, subfolder, task_id, fm["object_key"])
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=fm["data"],
                ContentType=fm["content_type"],
            )
            _logger.info("S3 upload OK: s3://%s/%s (%d bytes)", bucket, key, len(fm["data"]))
    except Exception:
        _logger.exception("S3 %s upload failed for task %s", subfolder, task_id)


class KenseiChatController(http.Controller):
    @http.route("/kensei/chat/create_turn", type="json", auth="user")
    def create_turn(
        self,
        sandbox_id=0,
        message="",
        model=None,
        timestamp="",
        is_hint=False,
        is_auto_hint=False,
        auto_hint_iteration=0,
        auto_hint_group_id="",
        attachments_meta="",
        **kw,
    ):
        sandbox_id = int(sandbox_id or 0)
        message = (message or "").strip()

        if not sandbox_id or not message:
            return {"error": "sandbox_id and message are required"}

        sandbox = request.env["kensei.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        if model is None:
            model = MODEL_DEFAULTS.get(sandbox.model_type, "unknown")

        next_num = len(sandbox.turn_ids) + 1
        is_hint_turn = bool(is_hint)
        vals = {
            "sandbox_id": sandbox.id,
            "turn_number": next_num,
            "model_name": model,
            "turn_status": "Pending",
            "is_hint_turn": is_hint_turn,
        }
        if is_hint_turn:
            vals["hints"] = message
        else:
            vals["prompt"] = message
        if timestamp:
            vals["prompt_timestamp"] = timestamp

        # Auto-hint metadata
        if is_auto_hint:
            vals["is_auto_hint"] = True
            vals["auto_hint_iteration"] = int(auto_hint_iteration or 0)
            if auto_hint_group_id:
                vals["auto_hint_group_id"] = auto_hint_group_id

        if attachments_meta:
            vals["attachments"] = attachments_meta

        turn = request.env["kensei.turn"].create(vals)

        if sandbox.session_status == "not_started":
            sandbox.sudo().write({"session_status": "in_progress"})

        return {"turn_id": turn.id}

    @http.route("/kensei/chat/save_response", type="json", auth="user")
    def save_response(
        self,
        turn_id=0,
        response="",
        tool_calls="",
        raw_events="",
        run_id="",
        timestamp="",
        partial=False,
        trajectory_input_tokens=0,
        trajectory_output_tokens=0,
        **kw,
    ):
        turn_id = int(turn_id or 0)

        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["kensei.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        vals = {
            "response": response or "",
        }
        # Never downgrade Completed → Streaming (late incremental save race)
        if partial:
            if turn.turn_status != "Completed":
                vals["turn_status"] = "Streaming"
        else:
            vals["turn_status"] = "Completed"
        if run_id:
            vals["run_id"] = run_id
        if timestamp:
            vals["response_timestamp"] = timestamp
        if tool_calls:
            vals["tool_calls"] = tool_calls
        if raw_events:
            vals["raw_events"] = raw_events
        if trajectory_input_tokens:
            vals["trajectory_input_tokens"] = int(trajectory_input_tokens)
        if trajectory_output_tokens:
            vals["trajectory_output_tokens"] = int(trajectory_output_tokens)

        turn.write(vals)

        return {"success": True}

    @http.route("/kensei/chat/mark_timeout", type="json", auth="user")
    def mark_timeout(self, turn_id=0, timeout_seconds=300, **kw):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["kensei.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        # Don't downgrade a Completed turn - late response won the race.
        if turn.turn_status == "Completed":
            return {"success": True, "status": "Completed", "already_completed": True}

        vals = {"turn_status": "TimedOut"}
        if not turn.response:
            vals["response"] = "[Response timed out after %ss]" % int(timeout_seconds or 300)
        turn.write(vals)

        _logger.warning(
            "Kensei chat turn %s marked TimedOut after %ss",
            turn_id,
            timeout_seconds,
        )
        return {"success": True, "status": "TimedOut"}

    @http.route("/kensei/chat/save_qc", type="json", auth="user")
    def save_qc(
        self,
        turn_id=0,
        severity="",
        qc_response="",
        dismiss_reason="",
        bedrock_input_tokens=0,
        bedrock_output_tokens=0,
        **kw,
    ):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["kensei.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        severity = (severity or "").strip().lower()
        valid = ("low", "medium", "high", "critical")
        if severity not in valid:
            return {"error": "Invalid severity: %s" % severity}

        vals = {
            "qc_severity": severity,
        }
        if qc_response:
            vals["qc_response"] = qc_response
        if dismiss_reason:
            vals["qc_dismiss_reason"] = dismiss_reason
        if bedrock_input_tokens:
            vals["bedrock_input_tokens"] = int(bedrock_input_tokens)
        if bedrock_output_tokens:
            vals["bedrock_output_tokens"] = int(bedrock_output_tokens)

        turn.write(vals)
        return {"success": True}

    @http.route("/kensei/chat/save_feedback", type="json", auth="user")
    def save_feedback(self, turn_id=0, feedback="", hint_text="", **kw):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["kensei.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        feedback = (feedback or "").strip().lower()
        if feedback not in ("satisfied", "unsatisfied"):
            return {"error": "Invalid feedback: %s" % feedback}

        vals = {"feedback": feedback}
        if hint_text:
            vals["hint_text"] = hint_text

        turn.write(vals)
        return {"success": True}

    @http.route("/kensei/chat/save_trajectory", type="json", auth="user")
    def save_trajectory(self, turn_id=0, trajectory_messages="", **kw):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["kensei.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        vals = {}
        if trajectory_messages:
            vals["trajectory_messages"] = trajectory_messages
            extracted = self._extract_tool_calls_from_trajectory(trajectory_messages)
            if extracted:
                existing_count = 0
                existing_has_results = False
                if turn.tool_calls:
                    try:
                        existing_list = json.loads(turn.tool_calls)
                        existing_count = len(existing_list)
                        existing_has_results = any(
                            tc.get("result")
                            for tc in existing_list
                            if isinstance(tc, dict)
                        )
                    except (json.JSONDecodeError, TypeError):
                        pass
                extracted_has_results = any(
                    tc.get("result") for tc in extracted if isinstance(tc, dict)
                )
                if len(extracted) > existing_count or (
                    extracted_has_results and not existing_has_results
                ):
                    vals["tool_calls"] = json.dumps(extracted)

            # Extract token usage from trajectory messages
            token_usage = self._extract_token_usage_from_trajectory(trajectory_messages)
            if token_usage["input_tokens"] > 0 or token_usage["output_tokens"] > 0:
                model_type = turn.sandbox_id.model_type if turn.sandbox_id else ""
                if model_type == "claude":
                    vals["claude_input_tokens"] = token_usage["input_tokens"]
                    vals["claude_output_tokens"] = token_usage["output_tokens"]
                elif model_type == "glm":
                    vals["glm_input_tokens"] = token_usage["input_tokens"]
                    vals["glm_output_tokens"] = token_usage["output_tokens"]
                elif model_type == "gpt":
                    vals["gpt_input_tokens"] = token_usage["input_tokens"]
                    vals["gpt_output_tokens"] = token_usage["output_tokens"]
                else:
                    vals["trajectory_input_tokens"] = token_usage["input_tokens"]
                    vals["trajectory_output_tokens"] = token_usage["output_tokens"]

        if vals:
            turn.write(vals)

        return {"success": True}

    @staticmethod
    def _extract_tool_calls_from_trajectory(trajectory_json):
        try:
            messages = json.loads(trajectory_json)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(messages, list):
            return []

        tool_calls = {}
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            inner = msg.get("message", msg)
            role = inner.get("role", "")
            content = inner.get("content", [])
            if not isinstance(content, list):
                continue

            if role == "assistant":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tc_id = block.get("id", "")
                        tool_calls[tc_id] = {
                            "toolCallId": tc_id,
                            "name": block.get("name", "unknown"),
                            "args": block.get("input", block.get("arguments", {})),
                            "result": None,
                            "isError": False,
                        }
                    elif isinstance(block, dict) and block.get("type") == "toolCall":
                        tc_id = block.get("id", "")
                        tool_calls[tc_id] = {
                            "toolCallId": tc_id,
                            "name": block.get("name", "unknown"),
                            "args": block.get("arguments", block.get("input", {})),
                            "result": None,
                            "isError": False,
                        }
            elif role in ("tool", "toolResult"):
                tc_id = inner.get("tool_use_id", inner.get("toolCallId", ""))
                if tc_id and tc_id in tool_calls:
                    result_text = ""
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            result_text += block.get("text", "")
                        elif isinstance(block, str):
                            result_text += block
                    tool_calls[tc_id]["result"] = result_text or None
                    tool_calls[tc_id]["isError"] = bool(
                        inner.get("is_error", inner.get("isError", False))
                    )

        return list(tool_calls.values())

    @staticmethod
    def _extract_token_usage_from_trajectory(trajectory_json):
        """Sum input_tokens / output_tokens from all messages in a trajectory.

        Anthropic API responses embed usage data in the message metadata as:
          {"usage": {"input_tokens": N, "output_tokens": N}}
        OpenClaw gateway wraps each message as:
          {"message": {..., "usage": {...}}, ...}
        """
        try:
            messages = json.loads(trajectory_json)
        except (json.JSONDecodeError, TypeError):
            return {"input_tokens": 0, "output_tokens": 0}
        if not isinstance(messages, list):
            return {"input_tokens": 0, "output_tokens": 0}

        total_in = 0
        total_out = 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage") or {}
            inner = msg.get("message")
            if isinstance(inner, dict):
                usage = usage or inner.get("usage") or {}
            total_in += int(usage.get("input_tokens", 0))
            total_out += int(usage.get("output_tokens", 0))

        return {"input_tokens": total_in, "output_tokens": total_out}

    @http.route("/kensei/chat/history", type="json", auth="user")
    def chat_history(self, sandbox_id=0, **kw):
        sandbox_id = int(sandbox_id or 0)

        if not sandbox_id:
            return {"error": "sandbox_id is required"}

        sandbox = request.env["kensei.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        turns = []
        for t in sandbox.turn_ids:
            tool_calls_str = t.tool_calls or ""
            if not tool_calls_str and t.trajectory_messages:
                extracted = self._extract_tool_calls_from_trajectory(
                    t.trajectory_messages
                )
                if extracted:
                    tool_calls_str = json.dumps(extracted)
                    t.sudo().write({"tool_calls": tool_calls_str})
            turns.append(
                {
                    "id": t.id,
                    "turn_number": t.turn_number,
                    "prompt": t.prompt or "",
                    "response": t.response or "",
                    "run_id": t.run_id or "",
                    "model": t.model_name or "",
                    "status": t.turn_status or "",
                    "prompt_timestamp": t.prompt_timestamp or "",
                    "response_timestamp": t.response_timestamp or "",
                    "tool_calls": tool_calls_str,
                    "qc_severity": t.qc_severity or "",
                    "qc_response": t.qc_response or "",
                    "qc_dismiss_reason": t.qc_dismiss_reason or "",
                    "feedback": t.feedback or "",
                    "hints": t.hints or "",
                    "hint_text": t.hint_text or "",
                    "is_hint_turn": t.is_hint_turn or False,
                    "attachments": t.attachments or "",
                }
            )

        output_artifacts = []
        try:
            output_artifacts = sandbox._build_output_artifacts()
        except Exception:
            _logger.debug("_build_output_artifacts failed for sandbox %s", sandbox_id)

        return {"turns": turns, "output_artifacts": output_artifacts}

    @http.route("/kensei/chat/export_session", type="http", auth="user")
    def export_session(self, sandbox_id=0, task_id=0, **kw):
        sandbox_id = int(sandbox_id or 0)
        task_id = int(task_id or 0)

        if sandbox_id:
            sandbox = request.env["kensei.sandbox"].browse(sandbox_id)
            if not sandbox.exists():
                return request.not_found()

            field_name = TRAJECTORY_FIELD_MAP.get(sandbox.model_type)
            saved = field_name and sandbox.kensei_id and sandbox.kensei_id[field_name]
            trajectory = None
            if saved:
                try:
                    parsed = json.loads(saved)
                    if isinstance(parsed, list) and parsed:
                        # Saved format: [{session_id, timestamp, trajectory}, ...]
                        # Extract the most recent session's trajectory.
                        trajectory = parsed[-1].get("trajectory")
                    elif isinstance(parsed, dict):
                        # Legacy format: {meta_info, messages} stored directly.
                        trajectory = parsed
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass

            if not trajectory:
                trajectory = sandbox.build_trajectory_json()

            content = json.dumps(trajectory, indent=2, ensure_ascii=False)

            label = sandbox.model_type or "sandbox"
            filename = "session-%s-%d.json" % (label, sandbox_id)
        elif task_id:
            task = request.env["kensei.kensei"].browse(task_id)
            if not task.exists():
                return request.not_found()
            trajectory = task.build_trajectory_json()
            content = json.dumps(trajectory, indent=2, ensure_ascii=False)
            filename = "session-%d.json" % task_id
        else:
            return request.not_found()

        return request.make_response(
            content,
            headers=[
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Disposition", 'attachment; filename="%s"' % filename),
            ],
        )

    @http.route("/kensei/chat/sandbox_state", type="json", auth="user")
    def sandbox_state(self, sandbox_id=0, **kw):
        sandbox_id = int(sandbox_id or 0)
        if not sandbox_id:
            return {"error": "sandbox_id is required"}

        sandbox = request.env["kensei.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        result = {
            "auto_hint_status": sandbox.auto_hint_status or "idle",
            "auto_hint_iteration": sandbox.auto_hint_iteration or 0,
            "auto_hint_group_id": sandbox.auto_hint_group_id or "",
        }

        last_turn = sandbox.turn_ids.sorted("turn_number", reverse=True)[:1]
        if last_turn:
            result["last_turn_id"] = last_turn.id
            result["last_turn_feedback"] = last_turn.feedback or ""
            result["last_turn_hint_text"] = last_turn.hint_text or ""
        else:
            result["last_turn_id"] = 0
            result["last_turn_feedback"] = ""
            result["last_turn_hint_text"] = ""

        return result

    @http.route("/kensei/chat/upload", type="http", auth="user", methods=["POST"], csrf=False)
    def upload_attachment(self, **kw):
        """Upload a file attachment, store in ir.attachment, return file ID."""
        file = request.httprequest.files.get("file")
        if not file:
            return request.make_json_response({"error": "No file provided"}, status=400)

        sandbox_id = int(kw.get("sandbox_id", 0))
        if not sandbox_id:
            return request.make_json_response({"error": "sandbox_id is required"}, status=400)

        sandbox = request.env["kensei.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return request.make_json_response({"error": "Sandbox not found"}, status=404)

        file_data = file.read()
        attachment = request.env["ir.attachment"].create({
            "name": file.filename,
            "type": "binary",
            "datas": base64.b64encode(file_data),
            "mimetype": file.content_type or "application/octet-stream",
            "res_model": "kensei.sandbox",
            "res_id": sandbox.id,
        })

        return request.make_json_response({
            "fileId": attachment.id,
            "name": attachment.name,
            "mimeType": attachment.mimetype,
            "size": len(file_data),
        })

    @http.route("/kensei/chat/persist_attachments", type="json", auth="user")
    def persist_attachments(self, sandbox_id=0, attachments=None, turn_id=0, **kw):
        sandbox_id = int(sandbox_id or 0)
        turn_id = int(turn_id or 0)
        if not sandbox_id or not attachments:
            return {"error": "sandbox_id and attachments are required"}

        sandbox = request.env["kensei.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        from ..models.kensei_sandbox_k8s import S3_KENSEI_PREFIX

        task = sandbox.kensei_id
        task_id = task.task_id or str(task.id)
        persona = task.persona_id
        persona_name = persona.name if persona else "marcus"

        persisted = []
        s3_files = []

        for att in attachments:
            name = att.get("fileName") or att.get("name") or "unnamed"
            mime_type = att.get("mimeType") or "application/octet-stream"
            content_b64 = att.get("content") or ""
            if not content_b64:
                continue

            try:
                file_bytes = base64.b64decode(content_b64)
            except Exception:
                _logger.warning("Failed to decode base64 for %s", name)
                continue

            safe_name = "%s_%s" % (uuid.uuid4().hex[:8], name.replace("/", "_").replace("\\", "_"))

            s3_files.append({
                "object_key": safe_name,
                "data": file_bytes,
                "content_type": mime_type,
            })

            persisted.append({"name": name, "storedAs": safe_name, "mimeType": mime_type, "size": len(file_bytes)})

        if s3_files:
            icp = request.env["ir.config_parameter"].sudo()
            bucket = icp.get_param("kensei.s3_bucket") or ""
            region = icp.get_param("kensei.s3_region") or "us-east-1"
            prefix = icp.get_param("kensei.s3_prefix") or S3_KENSEI_PREFIX
            access_key = os.environ.get("KENSEI_S3_ACCESS_KEY_ID") or os.environ.get("AWS_SECRET_KEY", "")
            secret_key = os.environ.get("KENSEI_S3_SECRET_ACCESS_KEY") or os.environ.get("AWS_ACCESS_SECRET_KEY", "")

            if bucket:
                t = threading.Thread(
                    target=_upload_to_s3_background,
                    args=(bucket, region, prefix, task_id, s3_files),
                    kwargs={"access_key": access_key, "secret_key": secret_key},
                    daemon=True,
                )
                t.start()
            else:
                _logger.info("S3 bucket not configured in Settings → skipping S3 upload")

        if turn_id and persisted:
            turn = request.env["kensei.turn"].browse(turn_id)
            if turn.exists():
                try:
                    existing = json.loads(turn.attachments or "[]")
                    if not isinstance(existing, list):
                        existing = []
                except (json.JSONDecodeError, TypeError):
                    existing = []
                for p in persisted:
                    matched = False
                    for e in existing:
                        if e.get("name") == p["name"]:
                            e["storedAs"] = p["storedAs"]
                            matched = True
                            break
                    if not matched:
                        existing.append(p)
                turn.sudo().write({"attachments": json.dumps(existing)})

        return {"success": True, "persisted": persisted}

    def _write_to_container(self, sandbox, persona_name, filename, file_bytes):
        mode = sandbox._deployment_mode()
        if mode == "k8s":
            self._write_to_k8s_pod(sandbox, filename, file_bytes)
        else:
            self._write_to_local_volume(sandbox, persona_name, filename, file_bytes)


    def _write_to_local_volume(self, sandbox, persona_name, filename, file_bytes):
        workdir = sandbox.docker_workdir
        if not workdir or not os.path.isdir(workdir):
            _logger.warning("Sandbox workdir not available: %s", workdir)
            return

        uploads_dir = os.path.join(workdir, "data", persona_name, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)

        filepath = os.path.join(uploads_dir, filename)
        with open(filepath, "wb") as f:
            f.write(file_bytes)
        _logger.info("Wrote attachment to container volume: %s (%d bytes)", filepath, len(file_bytes))

    def _write_to_k8s_pod(self, sandbox, filename, file_bytes):
        import subprocess
        import tempfile

        from ..models.kensei_sandbox_k8s import NAMESPACE

        pod_name = "kensei-sandbox-%s" % sandbox.kensei_id.id

        with tempfile.NamedTemporaryFile(delete=False, suffix="_" + filename) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        try:
            dest = "%s:/home/node/.openclaw/uploads/%s" % (pod_name, filename)
            subprocess.run(
                ["kubectl", "cp", tmp_path, dest, "-n", NAMESPACE, "-c", "openclaw"],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            _logger.info("kubectl cp OK: %s → %s", filename, dest)
        except Exception as e:
            _logger.warning("kubectl cp failed for %s: %s", filename, e)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @http.route("/kensei/chat/media", type="http", auth="user", methods=["GET"])
    def serve_media(self, sandbox_id=0, source="", **kw):
        sandbox_id = int(sandbox_id)
        sandbox = request.env["kensei.sandbox"].sudo().browse(sandbox_id)
        if not sandbox.exists():
            return request.not_found()
        task = sandbox.kensei_id
        user = request.env.user
        is_admin = user.has_group("kensei.group_kensei_ql")
        if not is_admin and user.id not in task.employee_ids.mapped("user_id").ids:
            return request.not_found()
        if not source or not source.startswith("/home/node/.openclaw/"):
            return request.not_found()
        relative = source[len("/home/node/.openclaw/"):]
        if ".." in relative or relative.startswith("/"):
            return request.not_found()

        data = None
        sandbox_running = sandbox.docker_status == "running"

        if sandbox_running:
            mode = request.env["ir.config_parameter"].sudo().get_param(
                "kensei.deployment_mode", "local"
            )
            if mode == "k8s":
                data = self._read_from_k8s_pod(sandbox, source)
            else:
                workdir = sandbox.docker_workdir
                if workdir:
                    persona = task.persona_id
                    persona_name = persona.name if persona else "marcus"
                    host_path = os.path.join(workdir, "data", persona_name, relative)
                    host_path = os.path.realpath(host_path)
                    allowed_base = os.path.realpath(os.path.join(workdir, "data", persona_name))
                    if host_path.startswith(allowed_base + os.sep) and os.path.isfile(host_path):
                        with open(host_path, "rb") as f:
                            data = f.read()

        if not data:
            data = self._read_artifact_from_s3(task, os.path.basename(source))

        if not data:
            return request.not_found()
        content_type, _ = mimetypes.guess_type(source)
        if not content_type:
            content_type = "application/octet-stream"
        headers = [
            ("Content-Type", content_type),
            ("Content-Disposition", "inline; filename=\"%s\"" % os.path.basename(source)),
            ("Cache-Control", "private, max-age=3600"),
        ]
        return request.make_response(data, headers=headers)

    @http.route("/kensei/chat/persist_output_media", type="json", auth="user")
    def persist_output_media(self, sandbox_id=0, media_paths=None, **kw):
        sandbox_id = int(sandbox_id or 0)
        if not sandbox_id or not media_paths:
            return {"success": False, "error": "sandbox_id and media_paths required"}

        sandbox = request.env["kensei.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return {"success": False, "error": "Sandbox not found"}

        task = sandbox.kensei_id
        task_id = task.task_id or str(task.id)
        persona_name = (task.persona_id.name or "default").strip().lower().replace(" ", "_")

        from ..models.kensei_sandbox_k8s import S3_KENSEI_PREFIX

        icp = request.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei.s3_bucket") or ""
        region = icp.get_param("kensei.s3_region") or "us-east-1"
        prefix = icp.get_param("kensei.s3_prefix") or S3_KENSEI_PREFIX
        access_key = os.environ.get("KENSEI_S3_ACCESS_KEY_ID") or os.environ.get("AWS_SECRET_KEY", "")
        secret_key = os.environ.get("KENSEI_S3_SECRET_ACCESS_KEY") or os.environ.get("AWS_ACCESS_SECRET_KEY", "")

        if not bucket:
            return {"success": False, "error": "S3 bucket not configured"}

        s3_files = []
        for source_path in (media_paths or []):
            if not source_path or not isinstance(source_path, str):
                continue
            file_bytes = self._read_media_from_container(sandbox, persona_name, source_path)
            if not file_bytes:
                _logger.warning("persist_output_media: could not read %s", source_path)
                continue
            filename = os.path.basename(source_path)
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            s3_files.append({
                "object_key": filename,
                "data": file_bytes,
                "content_type": content_type,
            })

        if s3_files:
            t = threading.Thread(
                target=_upload_to_s3_background,
                args=(bucket, region, prefix, task_id, s3_files, "output"),
                kwargs={"access_key": access_key, "secret_key": secret_key},
                daemon=True,
            )
            t.start()

        return {"success": True, "uploaded": len(s3_files)}

    def _read_media_from_container(self, sandbox, persona_name, container_path):
        mode = sandbox._deployment_mode()
        if mode == "k8s":
            return self._read_from_k8s_pod(sandbox, container_path)

        workdir = sandbox.docker_workdir
        if not workdir:
            return None

        base = os.path.join(workdir, "data", persona_name)
        relative = container_path.replace("/home/node/.openclaw/", "")
        host_path = os.path.realpath(os.path.join(base, relative))
        if not host_path.startswith(os.path.realpath(base)):
            _logger.warning("Path traversal blocked: %s", container_path)
            return None
        if not os.path.isfile(host_path):
            return None
        try:
            with open(host_path, "rb") as f:
                return f.read()
        except OSError as e:
            _logger.warning("Failed to read output media %s: %s", host_path, e)
            return None

    def _read_from_k8s_pod(self, sandbox, container_path):
        import subprocess
        import tempfile

        from ..models.kensei_sandbox_k8s import NAMESPACE

        pod_name = "kensei-sandbox-%s" % sandbox.kensei_id.id
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
        try:
            src = "%s:%s" % (pod_name, container_path)
            subprocess.run(
                ["kubectl", "cp", src, tmp_path, "-n", NAMESPACE, "-c", "openclaw"],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            with open(tmp_path, "rb") as f:
                return f.read()
        except Exception as e:
            _logger.warning("kubectl cp read failed for %s: %s", container_path, e)
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _read_artifact_from_s3(self, task, filename):
        from ..models.kensei_sandbox_k8s import S3_BUCKET, S3_KENSEI_PREFIX

        icp = request.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei.s3_bucket") or S3_BUCKET
        prefix = icp.get_param("kensei.s3_prefix") or S3_KENSEI_PREFIX
        if not bucket:
            return None

        task_id = task.task_id or str(task.id)
        access_key = os.environ.get("KENSEI_S3_ACCESS_KEY_ID") or os.environ.get("AWS_SECRET_KEY", "")
        secret_key = os.environ.get("KENSEI_S3_SECRET_ACCESS_KEY") or os.environ.get("AWS_ACCESS_SECRET_KEY", "")
        region = icp.get_param("kensei.s3_region") or "us-east-1"

        try:
            import boto3
            from botocore.config import Config as BotoConfig

            client_kwargs = {
                "region_name": region,
                "config": BotoConfig(retries={"max_attempts": 2, "mode": "adaptive"}),
            }
            if access_key and secret_key:
                client_kwargs["aws_access_key_id"] = access_key
                client_kwargs["aws_secret_access_key"] = secret_key

            s3 = boto3.client("s3", **client_kwargs)
            key = "%s/output/tasks/%s/%s" % (prefix, task_id, filename)
            resp = s3.get_object(Bucket=bucket, Key=key)
            return resp["Body"].read()
        except Exception:
            _logger.debug("S3 artifact fallback failed for %s/%s", task_id, filename)
            return None
