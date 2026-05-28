from odoo import http
from odoo.http import request


class WildclawChatBase(http.Controller):

    @http.route("/wildclaw_core/chat/send", type="json", auth="user", methods=["POST"])
    def send(self, sandbox_model, sandbox_id, message, **kwargs):
        Sandbox = request.env[sandbox_model]
        sandbox = Sandbox.browse(int(sandbox_id)).exists()
        if not sandbox:
            return {"error": "sandbox not found"}
        return {
            "status": "not_implemented",
            "message": "chat dispatch to be wired through wildclaw_core.services.wildclaw_runner.run_task() + ws_client",
            "sandbox_id": sandbox.id,
        }
