from odoo import http
from odoo.http import request


class WildclawGogAuth(http.Controller):

    @http.route("/wildclaw_core/gog_auth/start", type="json", auth="user", methods=["POST"])
    def start(self, **kwargs):
        return {"status": "not_implemented",
                "message": "google OAuth flow to be ported from kensei2/skoll_project/talos controllers/gog_auth.py (368 LOC)"}
