# -*- coding: utf-8 -*-
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class TalosChatController(http.Controller):
    @http.route("/talos/chat/create_turn", type="json", auth="user")
    def create_turn(self, task_id=0, message="", model="claude-opus-4.6", **kw):
        task_id = int(task_id or 0)
        message = (message or "").strip()

        if not task_id or not message:
            return {"error": "task_id and message are required"}

        task = request.env["talos.talos"].browse(task_id)
        if not task.exists():
            return {"error": "Task not found"}

        next_num = len(task.turn_ids) + 1
        turn = request.env["talos.turn"].create(
            {
                "talos_id": task.id,
                "turn_number": next_num,
                "prompt": message,
                "model_name": model,
                "turn_status": "Pending",
            }
        )

        return {"turn_id": turn.id}

    @http.route("/talos/chat/save_response", type="json", auth="user")
    def save_response(self, turn_id=0, response="", **kw):
        turn_id = int(turn_id or 0)

        if not turn_id:
            return {"error": "turn_id is required"}

        turn = request.env["talos.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        turn.write(
            {
                "response": response or "",
                "turn_status": "Completed",
            }
        )

        return {"success": True}

    @http.route("/talos/chat/history", type="json", auth="user")
    def chat_history(self, task_id=0, **kw):
        task_id = int(task_id or 0)

        if not task_id:
            return {"error": "task_id is required"}

        task = request.env["talos.talos"].browse(task_id)
        if not task.exists():
            return {"error": "Task not found"}

        turns = []
        for t in task.turn_ids:
            turns.append(
                {
                    "id": t.id,
                    "turn_number": t.turn_number,
                    "prompt": t.prompt or "",
                    "response": t.response or "",
                    "run_id": t.run_id or "",
                    "model": t.model_name or "",
                    "status": t.turn_status or "",
                }
            )

        return {"turns": turns}
