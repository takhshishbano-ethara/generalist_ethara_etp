import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class IntentTestGeneration(http.Controller):

    @http.route("/kensei_wildclaw/intent_test/generate", type="json", auth="user", methods=["POST"])
    def generate(self, task_id, **kwargs):
        task = request.env["kensei_wildclaw.task"].browse(int(task_id)).exists()
        if not task:
            return {"error": "task not found"}
        task.write({"intent_test_status": "generating"})
        return {"task_id": task.id, "status": "queued"}

    @http.route("/kensei_wildclaw/intent_test/status/<int:task_id>", type="json", auth="user", methods=["POST"])
    def status(self, task_id, **kwargs):
        task = request.env["kensei_wildclaw.task"].browse(int(task_id)).exists()
        if not task:
            return {"error": "task not found"}
        return {
            "task_id": task.id,
            "status": task.intent_test_status,
            "intent_test_jsonl_length": len(task.intent_test_jsonl or ""),
        }
