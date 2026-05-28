import json

from odoo import http
from odoo.http import request


class TalosExport(http.Controller):

    @http.route("/talos_wildclaw/export/<int:task_id>", type="http", auth="user")
    def export_task(self, task_id, **kwargs):
        task = request.env["talos_wildclaw.task"].browse(int(task_id)).exists()
        if not task:
            return request.not_found()
        payload = {
            "task_id": task.task_id,
            "name": task.name,
            "persona": task.persona_id.name if task.persona_id else "",
            "task_type": task.task_type,
            "difficulty": task.difficulty,
            "system_prompt": task.system_prompt or "",
            "seed_prompt": task.seed_prompt or "",
            "initial_prompt": task.initial_prompt or "",
            "sandboxes": [{
                "model_type": s.model_type,
                "variant_index": s.variant_index,
                "trajectory_status": s.trajectory_status,
                "trajectory_jsonl": s.trajectory_jsonl or "",
            } for s in task.sandbox_ids],
        }
        return request.make_response(
            json.dumps(payload, indent=2),
            headers=[
                ("Content-Type", "application/json"),
                ("Content-Disposition", f'attachment; filename="talos_task_{task.task_id}.json"'),
            ],
        )
