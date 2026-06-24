from __future__ import annotations

from odoo import http
from odoo.http import request


class LynceusTaskerController(http.Controller):

    @http.route("/lynceus/health", type="http", auth="public", csrf=False)
    def health(self, **_kw):
        return request.make_json_response({"status": "ok", "module": "lynceus"})
