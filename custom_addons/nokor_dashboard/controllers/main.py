import json
import os

from odoo import http
from odoo.http import request


class NokorShowcaseController(http.Controller):

    @http.route("/nokor", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "github_url": ICP.get_param(
                "nokor_dashboard.github_url", ""
            ) or "https://github.com/Ethara-Ai/Nokor",
            "dataset_url": ICP.get_param(
                "nokor_dashboard.dataset_url", ""
            ) or "https://huggingface.co/datasets/ethara/Nokor",
        }
        return request.render("nokor_dashboard.portal_showcase", values)

    @http.route("/nokor/api/dataset", type="http", auth="public", website=True, sitemap=False)
    def api_dataset(self, **kw):
        module_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        json_path = os.path.join(module_path, "data", "nokor_instances.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = f.read()
        except (FileNotFoundError, IOError):
            data = "[]"
        return request.make_response(
            data,
            headers=[("Content-Type", "application/json")],
        )
