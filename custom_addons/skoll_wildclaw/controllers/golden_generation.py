import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class GoldenGeneration(http.Controller):

    @http.route("/skoll_wildclaw/golden/generate", type="json", auth="user", methods=["POST"])
    def generate(self, task_id, **kwargs):
        task = request.env["skoll_wildclaw.task"].browse(int(task_id)).exists()
        if not task:
            return {"error": "task not found"}
        task.write({"golden_status": "generating", "golden_error": False})
        return {"task_id": task.id, "status": "queued"}

    @http.route("/skoll_wildclaw/golden/status/<int:task_id>", type="json", auth="user", methods=["POST"])
    def status(self, task_id, **kwargs):
        task = request.env["skoll_wildclaw.task"].browse(int(task_id)).exists()
        if not task:
            return {"error": "task not found"}
        return {
            "task_id": task.id,
            "status": task.golden_status,
            "has_golden": bool(task.golden_trajectory),
            "error": task.golden_error or "",
        }
