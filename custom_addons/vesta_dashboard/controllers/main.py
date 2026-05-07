import json
import os

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "vesta_tasks.json",
)


class VestaShowcaseController(http.Controller):

    @http.route("/vesta", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "github_url": ICP.get_param(
                "vesta_dashboard.github_url", ""
            ) or "https://github.com/Ethara-Ai/Vesta",
            "dataset_url": ICP.get_param(
                "vesta_dashboard.dataset_url", ""
            ) or "https://huggingface.co/datasets/ethara/Vesta",
        }
        return request.render("vesta_dashboard.portal_showcase", values)

    @http.route("/vesta/api/tasks", type="http", auth="public", cors="*")
    def api_tasks(self, **kw):
        try:
            with open(_DATA_PATH, "r") as f:
                data = f.read()
        except FileNotFoundError:
            data = "[]"
        return http.Response(
            data, content_type="application/json", status=200
        )
