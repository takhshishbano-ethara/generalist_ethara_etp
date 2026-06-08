"""Aurora resources tab: external links, returned as a block-schema table
rendered generically by the frontend. Sourced from the seeded
aurora.showcase.resource model.
"""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import user_role_tag

RESOURCE_COLUMNS = [
    {"key": "label", "label": "Label", "type": "string"},
    {"key": "title", "label": "Resource", "type": "string"},
    {"key": "url", "label": "Link", "type": "url"},
]


class AuroraContentController(http.Controller):

    @http.route(
        "/api/v1/aurora_ext/content",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def aurora_ext_content(self, **kwargs):
        env = request.env
        role_tag = user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Aurora data.",
                status=403,
            )

        resource_rows = [
            r.to_dict()
            for r in env["aurora.showcase.resource"].sudo().search([])
        ]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag,
                "blocks": [
                    {
                        "type": "table",
                        "title": "Resources",
                        "columns": RESOURCE_COLUMNS,
                        "rows": resource_rows,
                    },
                ],
            },
        )
