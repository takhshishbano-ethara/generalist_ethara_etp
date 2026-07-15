import os

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "kaiju_instances.json",
)


class KaijuShowcaseController(http.Controller):

    @http.route("/kaiju", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "trajectories_url": ICP.get_param(
                "kaiju_dashboard.trajectories_url", ""
            ) or "https://github.com/EtharaOrion/kaiju-samples.git",
            "dataset_url": ICP.get_param(
                "kaiju_dashboard.dataset_url", ""
            ) or "https://huggingface.co/datasets/ethara/kaiju-samples",
        }
        return request.render("kaiju_dashboard.portal_showcase", values)

    @http.route(
        "/kaiju/api/instances", type="http", auth="public", website=True, sitemap=False
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
