from odoo import http
from odoo.http import request


class WildclawBrowserAuth(http.Controller):

    @http.route("/wildclaw_core/browser_auth/start", type="json", auth="user", methods=["POST"])
    def start(self, **kwargs):
        return {"status": "not_implemented",
                "message": "browser_auth flow to be ported from kensei2/skoll_project/talos controllers/browser_auth.py"}
