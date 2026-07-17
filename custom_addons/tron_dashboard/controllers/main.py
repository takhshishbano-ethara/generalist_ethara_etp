import os

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "tron_instances.json",
)


class TronShowcaseController(http.Controller):

    @http.route("/tron", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "trajectories_url": ICP.get_param(
                "tron_dashboard.trajectories_url", ""
            ) or "https://example.com/tron-repo",
            "dataset_url": ICP.get_param(
                "tron_dashboard.dataset_url", ""
            ) or "https://example.com/tron-dataset",
        }
        return request.render("tron_dashboard.portal_showcase", values)

    @http.route(
        "/tron/api/instances", type="http", auth="public", website=True, sitemap=False
    )
    def api_instances(self, **kw):
        try:
            with open(_DATA_PATH, "r", encoding="utf-8") as f:
                data = f.read()
        except (FileNotFoundError, IOError):
            data = "[]"
        return request.make_response(
            data, headers=[("Content-Type", "application/json")]
        )
