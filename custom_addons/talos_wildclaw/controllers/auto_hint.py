from odoo import http
from odoo.http import request


class TalosAutoHint(http.Controller):

    @http.route("/talos_wildclaw/auto_hint/trigger", type="json", auth="user", methods=["POST"])
    def trigger(self, sandbox_id, **kwargs):
        sandbox = request.env["talos_wildclaw.sandbox"].browse(int(sandbox_id)).exists()
        if not sandbox:
            return {"error": "sandbox not found"}
        sandbox.write({"auto_hint_status": "evaluating"})
        return {"sandbox_id": sandbox.id, "status": "queued"}

    @http.route("/talos_wildclaw/auto_hint/status/<int:sandbox_id>", type="json", auth="user", methods=["POST"])
    def status(self, sandbox_id, **kwargs):
        sandbox = request.env["talos_wildclaw.sandbox"].browse(int(sandbox_id)).exists()
        if not sandbox:
            return {"error": "sandbox not found"}
        return {
            "sandbox_id": sandbox.id,
            "auto_hint_status": sandbox.auto_hint_status,
            "auto_hint_iteration": sandbox.auto_hint_iteration,
        }
