import os

from odoo import http
from odoo.http import request


class RaidenShowcaseController(http.Controller):
    @http.route("/raiden", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "trajectories_url": ICP.get_param("raiden_dashboard.trajectories_url", "")
            or "https://github.com/harbor-framework/harbor",
            "dataset_url": ICP.get_param("raiden_dashboard.dataset_url", "")
            or "https://github.com/Ethara-Ai/software-agent-sdk",
        }
        return request.render("raiden_dashboard.portal_showcase", values)

    @http.route(
        "/raiden/api/instances", type="http", auth="public", website=True, sitemap=False
    )
    def api_instances(self, **kw):
        module_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        json_path = os.path.join(module_path, "data", "raiden_instances.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = f.read()
        except (FileNotFoundError, IOError):
            data = "[]"
        return request.make_response(
            data,
            headers=[("Content-Type", "application/json")],
        )
