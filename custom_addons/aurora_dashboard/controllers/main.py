import json
import os

from odoo import http
from odoo.http import request


class AuroraShowcaseController(http.Controller):

    @http.route("/milobench", type="http", auth="public", website=True, sitemap=True)
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "trajectories_url": ICP.get_param(
                "aurora_dashboard.trajectories_url", ""
            ) or "https://github.com/Ethara-Ai/milo-bench-paper/tree/main/trajectories",
            "dataset_url": ICP.get_param(
                "aurora_dashboard.dataset_url", ""
            ) or "https://huggingface.co/datasets/ethara/MILO-Bench",
        }
        return request.render("aurora_dashboard.portal_showcase", values)

    @http.route("/aurora/api/delivery-evaluation", type="http", auth="public", website=True, sitemap=False)
    def api_delivery_evaluation(self, **kw):
        module_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        json_path = os.path.join(module_path, "data", "delivery_evaluation.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = f.read()
        except (FileNotFoundError, IOError):
            data = "[]"
        return request.make_response(
            data,
            headers=[("Content-Type", "application/json")],
        )
