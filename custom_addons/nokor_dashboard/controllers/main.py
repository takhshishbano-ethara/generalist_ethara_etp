import json
import os

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "nokor_instances.json",
)


class TerraShowcaseController(http.Controller):

    @http.route("/nokor", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        return self._get_showcase_response()

    @http.route("/terra", type="http", auth="public", sitemap=False)
    def terra_page(self, **kw):
        return self._get_showcase_response()

    def _get_showcase_response(self):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "github_url": ICP.get_param(
                "nokor_dashboard.github_url", ""
            ) or "https://github.com/Ethara-Ai/terra",
            "dataset_url": ICP.get_param(
                "nokor_dashboard.dataset_url", ""
            ) or "https://huggingface.co/datasets/ethara/terra",
        }
        return request.render("nokor_dashboard.portal_showcase", values)

    @http.route("/nokor/api/dataset", type="http", auth="public", cors="*", csrf=False)
    def api_dataset(self, **kw):
        return self._get_dataset_response()

    @http.route("/terra/api/dataset", type="http", auth="public", cors="*", csrf=False)
    def terra_api_dataset(self, **kw):
        return self._get_dataset_response()

    def _get_dataset_response(self):
        try:
            with open(_DATA_PATH, "r", encoding="utf-8") as f:
                data = f.read()
        except (FileNotFoundError, IOError):
            data = "[]"
        return request.make_response(
            data,
            headers=[("Content-Type", "application/json")],
        )
