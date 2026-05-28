import json
import time

from odoo import http
from odoo.http import request


class KenseiWildclawSSE(http.Controller):

    @http.route("/kensei_wildclaw/sse/<int:sandbox_id>", type="http", auth="user")
    def sse_stream(self, sandbox_id, **kwargs):
        sandbox = request.env["kensei_wildclaw.sandbox"].browse(int(sandbox_id)).exists()
        if not sandbox:
            return request.not_found()

        def gen():
            yield "retry: 1500\n\n"
            for _ in range(60):
                payload = {
                    "sandbox_id": sandbox.id,
                    "status": sandbox.trajectory_status,
                    "session_status": sandbox.session_status,
                }
                yield f"data: {json.dumps(payload)}\n\n"
                time.sleep(1)
                if sandbox.trajectory_status in ("done", "error"):
                    break

        headers = [
            ("Content-Type", "text/event-stream"),
            ("Cache-Control", "no-cache"),
            ("X-Accel-Buffering", "no"),
        ]
        return request.make_response(gen(), headers=headers)
