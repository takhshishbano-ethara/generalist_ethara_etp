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

    @http.route("/talos/chat/save_trajectory", type="json", auth="user")
    def save_trajectory(self, turn_id=0, trajectory_messages="", **kw):
        turn_id = int(turn_id or 0)
        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["talos.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        if trajectory_messages:
            turn.write({"trajectory_messages": trajectory_messages})

        return {"success": True}

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
            turns.append(
                {
                    "id": t.id,
                    "turn_number": t.turn_number,
                    "prompt": t.prompt or "",
                    "response": t.response or "",
                    "run_id": t.run_id or "",
                    "model": t.model_name or "",
                    "status": t.turn_status or "",
                    "tool_calls": t.tool_calls or "",
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
