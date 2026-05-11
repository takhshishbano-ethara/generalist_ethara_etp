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
    """Public-facing routes for the Valkyrie showcase.

    The template is rendered as a bare HTML document — no
    ``portal.portal_layout`` wrapper, no ``web.assets_frontend``
    bundle — so Bootstrap + the Odoo website footer/footer-chrome
    don't fight the module's own design system.
    """

    @http.route(["/valkyrie", "/Valkyrie"], type="http", auth="public", website=True, sitemap=True)
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
        rendered = request.env["ir.qweb"]._render(
            "valkyrie_dashboard.portal_showcase", values
        )
        return request.make_response(
            "<!DOCTYPE html>\n" + str(rendered),
            headers=[("Content-Type", "text/html; charset=utf-8")],
        )

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
