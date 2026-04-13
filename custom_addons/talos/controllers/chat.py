# -*- coding: utf-8 -*-
import json
import logging

from odoo import http
from odoo.http import request

from ..models.talos_sandbox import MODEL_DEFAULTS

_logger = logging.getLogger(__name__)


class TalosChatController(http.Controller):
    @http.route("/talos/chat/create_turn", type="json", auth="user")
    def create_turn(self, sandbox_id=0, message="", model=None, **kw):
        sandbox_id = int(sandbox_id or 0)
        message = (message or "").strip()

        if not sandbox_id or not message:
            return {"error": "sandbox_id and message are required"}

        sandbox = request.env["talos.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        if model is None:
            model = MODEL_DEFAULTS.get(sandbox.model_type, "unknown")

        next_num = len(sandbox.turn_ids) + 1
        turn = request.env["talos.turn"].create(
            {
                "sandbox_id": sandbox.id,
                "turn_number": next_num,
                "prompt": message,
                "model_name": model,
                "turn_status": "Pending",
            }
        )

        if sandbox.session_status == "not_started":
            sandbox.sudo().write({"session_status": "in_progress"})

        return {"turn_id": turn.id}

    @http.route("/talos/chat/save_response", type="json", auth="user")
    def save_response(self, turn_id=0, response="", tool_calls="", raw_events="", **kw):
        turn_id = int(turn_id or 0)

        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["talos.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        vals = {
            "response": response or "",
            "turn_status": "Completed",
        }
        if tool_calls:
            vals["tool_calls"] = tool_calls
        if raw_events:
            vals["raw_events"] = raw_events

        turn.write(vals)

        return {"success": True}

    @http.route("/talos/chat/save_qc", type="json", auth="user")
    def save_qc(self, turn_id=0, severity="", qc_response="", dismiss_reason="", **kw):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["talos.turn"].browse(turn_id)
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

        turn.write(vals)
        return {"success": True}

    @http.route("/talos/chat/save_trajectory", type="json", auth="user")
    def save_trajectory(self, turn_id=0, trajectory_messages="", **kw):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["talos.turn"].browse(turn_id)
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
                            tc.get("result") for tc in existing_list if isinstance(tc, dict)
                        )
                    except (json.JSONDecodeError, TypeError):
                        pass
                extracted_has_results = any(
                    tc.get("result") for tc in extracted if isinstance(tc, dict)
                )
                if (len(extracted) > existing_count
                    or (extracted_has_results and not existing_has_results)):
                    vals["tool_calls"] = json.dumps(extracted)

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
                    tool_calls[tc_id]["isError"] = bool(inner.get("is_error", inner.get("isError", False)))

        return list(tool_calls.values())

    @http.route("/talos/chat/history", type="json", auth="user")
    def chat_history(self, sandbox_id=0, **kw):
        sandbox_id = int(sandbox_id or 0)

        if not sandbox_id:
            return {"error": "sandbox_id is required"}

        sandbox = request.env["talos.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        turns = []
        for t in sandbox.turn_ids:
            tool_calls_str = t.tool_calls or ""
            if not tool_calls_str and t.trajectory_messages:
                extracted = self._extract_tool_calls_from_trajectory(t.trajectory_messages)
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
                    "tool_calls": tool_calls_str,
                    "qc_severity": t.qc_severity or "",
                    "qc_response": t.qc_response or "",
                    "qc_dismiss_reason": t.qc_dismiss_reason or "",
                }
            )

        return {"turns": turns}

    @http.route("/talos/chat/export_session", type="http", auth="user")
    def export_session(self, sandbox_id=0, task_id=0, **kw):
        sandbox_id = int(sandbox_id or 0)
        task_id = int(task_id or 0)

        if sandbox_id:
            sandbox = request.env["talos.sandbox"].browse(sandbox_id)
            if not sandbox.exists():
                return request.not_found()
            trajectory = sandbox.build_trajectory_json()
            label = sandbox.model_type or "sandbox"
            filename = "session-%s-%d.json" % (label, sandbox_id)
        elif task_id:
            task = request.env["talos.talos"].browse(task_id)
            if not task.exists():
                return request.not_found()
            trajectory = task.build_trajectory_json()
            filename = "session-%d.json" % task_id
        else:
            return request.not_found()

        content = json.dumps(trajectory, indent=2, ensure_ascii=False)

        return request.make_response(
            content,
            headers=[
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Disposition", 'attachment; filename="%s"' % filename),
            ],
        )
