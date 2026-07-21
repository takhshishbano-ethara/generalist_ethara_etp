import os

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "kang_instances.json",
)


class KangShowcaseController(http.Controller):

    @http.route("/kang", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "trajectories_url": ICP.get_param(
                "kang_dashboard.trajectories_url", ""
            ) or "https://github.com/EtharaOrion/kang-samples.git",
            "dataset_url": ICP.get_param(
                "kang_dashboard.dataset_url", ""
            ) or "https://huggingface.co/datasets/ethara/kang",
        }
        return request.render("kang_dashboard.portal_showcase", values)

    @http.route(
        "/kang/api/instances", type="http", auth="public", website=True, sitemap=False
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
