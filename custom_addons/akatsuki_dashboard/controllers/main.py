import json
import os

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "akatsuki_instances.json",
)


class AkatsukiShowcaseController(http.Controller):

    @http.route("/akatsuki", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "trajectories_url": ICP.get_param(
                "akatsuki_dashboard.trajectories_url", ""
            ) or "https://github.com/Ethara-Ai/akatsuki",
            "dataset_url": ICP.get_param(
                "akatsuki_dashboard.dataset_url", ""
            ) or "https://huggingface.co/datasets/ethara/Akatsuki",
        }
        return request.render("akatsuki_dashboard.portal_showcase", values)

    @http.route("/akatsuki/api/instances", type="http", auth="public", cors="*")
    def api_instances(self, **kw):
        try:
            with open(_DATA_PATH, "r") as f:
                data = f.read()
        except FileNotFoundError:
            data = "[]"
        return http.Response(
            data, content_type="application/json", status=200
        )
