from odoo import http
from odoo.http import request


_DEFAULTS = {
    "kaiju": "https://projects.ethara.ai/kaiju",
    "kraken": "https://projects.ethara.ai/kraken",
    "aurora": "https://projects.ethara.ai/aurora",
    "valkyrie": "https://projects.ethara.ai/valkyrie",
    "tesseract": "https://projects.ethara.ai/tesseract",
}


class EtharaProjectsHubController(http.Controller):

    @http.route("/projects", type="http", auth="public", website=True, sitemap=True)
    def projects_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        projects = []
        for key, default_url in _DEFAULTS.items():
            url = ICP.get_param(
                f"ethara_projects_hub.{key}_url", ""
            ) or default_url
            projects.append({
                "key": key,
                "name": key.capitalize(),
                "url": url,
            })
        values = {"projects": projects}
        return request.render("ethara_projects_hub.portal_projects_hub", values)
