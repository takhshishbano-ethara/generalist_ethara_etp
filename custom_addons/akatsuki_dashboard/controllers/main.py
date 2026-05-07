import json
import os

from markupsafe import Markup

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
        try:
            with open(_DATA_PATH, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            raw_data = []

        instances_json = Markup(json.dumps(raw_data, ensure_ascii=False))

        values = {
            "trajectories_url": ICP.get_param(
                "akatsuki_dashboard.trajectories_url", ""
            ) or "https://github.com/Ethara-Ai/akatsuki",
            "dataset_url": ICP.get_param(
                "akatsuki_dashboard.dataset_url", ""
            ) or "https://huggingface.co/datasets/ethara/Akatsuki",
            "instances_json": instances_json,
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
