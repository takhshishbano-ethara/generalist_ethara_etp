# -*- coding: utf-8 -*-
import json
import logging

from odoo import http
from odoo.http import request

from ..models.atlas_sandbox import MODEL_DEFAULTS, TRAJECTORY_FIELD_MAP

_logger = logging.getLogger(__name__)


class AtlasChatController(http.Controller):
    @http.route("/atlas/chat/create_turn", type="json", auth="user")
    def create_turn(
        self, sandbox_id=0, message="", model=None, timestamp="", is_hint=False, **kw
    ):
        sandbox_id = int(sandbox_id or 0)
        message = (message or "").strip()

        if not sandbox_id or not message:
            return {"error": "sandbox_id and message are required"}

        sandbox = request.env["atlas.sandbox"].browse(sandbox_id)
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

        turn = request.env["atlas.turn"].create(vals)

        if sandbox.session_status == "not_started":
            sandbox.sudo().write({"session_status": "in_progress"})

        return {"turn_id": turn.id}

    @http.route("/atlas/chat/save_response", type="json", auth="user")
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

        turn = request.env["atlas.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        vals = {
            "response": response or "",
            "turn_status": "Streaming" if partial else "Completed",
        }
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

    @http.route("/atlas/chat/save_qc", type="json", auth="user")
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

        turn = request.env["atlas.turn"].browse(turn_id)
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

    @http.route("/atlas/chat/save_feedback", type="json", auth="user")
    def save_feedback(self, turn_id=0, feedback="", hint_text="", **kw):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["atlas.turn"].browse(turn_id)
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

    @http.route("/atlas/chat/save_trajectory", type="json", auth="user")
    def save_trajectory(self, turn_id=0, trajectory_messages="", **kw):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["atlas.turn"].browse(turn_id)
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
                if model_type == "glm":
                    vals["glm_input_tokens"] = token_usage["input_tokens"]
                    vals["glm_output_tokens"] = token_usage["output_tokens"]
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

    @http.route("/atlas/chat/history", type="json", auth="user")
    def chat_history(self, sandbox_id=0, **kw):
        sandbox_id = int(sandbox_id or 0)

        if not sandbox_id:
            return {"error": "sandbox_id is required"}

        sandbox = request.env["atlas.sandbox"].browse(sandbox_id)
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
                }
            )

        return {"turns": turns}

    @http.route("/atlas/chat/export_session", type="http", auth="user")
    def export_session(self, sandbox_id=0, task_id=0, **kw):
        sandbox_id = int(sandbox_id or 0)
        task_id = int(task_id or 0)

        if sandbox_id:
            sandbox = request.env["atlas.sandbox"].browse(sandbox_id)
            if not sandbox.exists():
                return request.not_found()

            field_name = TRAJECTORY_FIELD_MAP.get(sandbox.model_type)
            saved = field_name and sandbox.atlas_id and sandbox.atlas_id[field_name]
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
            task = request.env["atlas.atlas"].browse(task_id)
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
