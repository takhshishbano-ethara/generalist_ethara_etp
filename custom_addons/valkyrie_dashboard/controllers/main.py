import json
import os

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "valkyrie_instances.json",
)


class ValkyrieShowcaseController(http.Controller):

    @http.route("/valkyrie", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "github_url": ICP.get_param(
                "valkyrie_dashboard.github_url", ""
            ) or "https://github.com/Ethara-Ai/Valkyrie",
            "dataset_url": ICP.get_param(
                "valkyrie_dashboard.dataset_url", ""
            ) or "https://huggingface.co/datasets/ethara/Valkyrie",
        }
        return request.render("valkyrie_dashboard.portal_showcase", values)

    @http.route("/valkyrie/api/instances", type="http", auth="public", cors="*")
    def api_instances(self, **kw):
        try:
            with open(_DATA_PATH, "r") as f:
                data = f.read()
        except FileNotFoundError:
            data = "[]"
        return http.Response(
            data, content_type="application/json", status=200
        )
