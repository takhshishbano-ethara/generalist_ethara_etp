"""Aurora analytics tab: per-model comparison, returned as a block schema —
a comparison table + an overall pass-rate bar chart. Rendered generically by
the frontend. Sourced from the seeded aurora.showcase.model.stat model.
"""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import user_role_tag

MODEL_COLUMNS = [
    {"key": "name", "label": "Model", "type": "string"},
    {"key": "overall_pass_rate", "label": "Overall Pass Rate", "type": "string"},
    {"key": "short_trajectories", "label": "Short Trajectories", "type": "string"},
    {"key": "long_trajectories", "label": "Long Trajectories", "type": "string"},
    {"key": "avg_cost_per_instance", "label": "Avg Cost / Instance", "type": "string"},
]


def _pct_to_float(value):
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


class AuroraAnalyticsController(http.Controller):

    @http.route(
        "/api/v1/aurora_ext/analytics_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def aurora_ext_analytics_dashboard(self, **kwargs):
        env = request.env
        role_tag = user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Aurora data.",
                status=403,
            )

        model_rows = [
            s.to_dict()
            for s in env["aurora.showcase.model.stat"].sudo().search([])
        ]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag,
                "blocks": [
                    {
                        "type": "table",
                        "title": "Model comparison",
                        "columns": MODEL_COLUMNS,
                        "rows": model_rows,
                    },
                    {
                        "type": "chart",
                        "variant": "bar",
                        "title": "Overall pass rate",
                        "items": [
                            {
                                "label": r.get("name", ""),
                                "value": r.get("overall_pass_rate", ""),
                                "percent": _pct_to_float(
                                    r.get("overall_pass_rate")
                                ),
                            }
                            for r in model_rows
                        ],
                    },
                ],
            },
        )
