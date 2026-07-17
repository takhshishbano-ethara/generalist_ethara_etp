import os

from odoo import http
from odoo.http import request


class RinzlerShowcaseController(http.Controller):
    @http.route("/rinzler", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "harness_url": ICP.get_param("rinzler_dashboard.harness_url", "")
            or "https://github.com/Ethara-ai/yc-bench",
            "dataset_url": ICP.get_param("rinzler_dashboard.dataset_url", "")
            or "https://github.com/Ethara-Ai/rinzler-dataset",
        }
        return request.render("rinzler_dashboard.portal_showcase", values)

    @http.route(
        "/rinzler/api/instances",
        type="http",
        auth="public",
        website=True,
        sitemap=False,
    )
    def api_instances(self, **kw):
        module_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        json_path = os.path.join(module_path, "data", "rinzler_instances.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = f.read()
        except (FileNotFoundError, IOError):
            data = "[]"
        return request.make_response(
            data,
            headers=[("Content-Type", "application/json")],
        )
