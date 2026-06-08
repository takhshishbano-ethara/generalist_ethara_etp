"""Aurora overview tab: returns a block-based schema (kpi + chart) that the
frontend renders generically — KPIs as cards, the status distribution as a bar
chart. Computed live from aurora.evaluation.instance records.
"""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import IN_PROGRESS_STATES, pct, user_role_tag


def _kpi_item(key, label, value, sub_string=""):
    return {
        "key": key,
        "label": label,
        "value": value,
        "sub_string": sub_string,
    }


def _compute_kpi(env):
    Instance = env["aurora.evaluation.instance"].sudo()
    Evaluation = env["aurora.evaluation"].sudo()

    total = Instance.search_count([])
    resolved = Instance.search_count([("resolved", "=", True)])
    errored = Instance.search_count([("status", "=", "error")])
    in_progress = Instance.search_count(
        [("status", "in", list(IN_PROGRESS_STATES))]
    )
    repo_rows = Instance.read_group(
        [], fields=["org", "repo"], groupby=["org", "repo"], lazy=False
    )
    repos_covered = len(repo_rows)
    eval_runs = Evaluation.search_count([])

    return [
        _kpi_item(
            "total_instances", "Total Instances", str(total),
            f"{eval_runs} evaluation run(s)",
        ),
        _kpi_item(
            "resolved", "Resolved", f"{resolved}/{total}",
            f"{pct(resolved, total)}% resolve rate",
        ),
        _kpi_item(
            "in_progress", "In Progress", str(in_progress), f"{errored} errored",
        ),
        _kpi_item(
            "repos_covered", "Repos Covered", str(repos_covered),
            "distinct org/repo pairs",
        ),
    ]


def _status_chart_items(env):
    Instance = env["aurora.evaluation.instance"].sudo()
    total = Instance.search_count([])
    buckets = [
        ("Resolved", Instance.search_count([("resolved", "=", True)])),
        ("Unresolved", Instance.search_count([("status", "=", "unresolved")])),
        ("In Progress",
         Instance.search_count([("status", "in", list(IN_PROGRESS_STATES))])),
        ("Error", Instance.search_count([("status", "=", "error")])),
    ]
    return [
        {"label": label, "value": count, "percent": pct(count, total)}
        for label, count in buckets
    ]


class AuroraDashboardOverviewController(http.Controller):

    @http.route(
        "/api/v1/aurora_ext/dashboard_overview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def aurora_ext_dashboard_overview(self, **kwargs):
        env = request.env
        role_tag = user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Aurora data.",
                status=403,
            )

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag,
                "blocks": [
                    {"type": "kpi", "items": _compute_kpi(env)},
                    {
                        "type": "chart",
                        "variant": "bar",
                        "title": "Instances by status",
                        "items": _status_chart_items(env),
                    },
                ],
            },
        )
