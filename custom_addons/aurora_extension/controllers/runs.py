"""Aurora runs tab (the 'tasks/jobs' equivalent): evaluations + pipelines,
returned as a block schema — a KPI summary + a table of runs. Rendered
generically by the frontend (kpi cards + table).
"""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import user_role_tag

RUN_COLUMNS = [
    {"key": "kind", "label": "Type", "type": "string"},
    {"key": "name", "label": "Name", "type": "string"},
    {"key": "stage", "label": "Stage", "type": "string"},
    {"key": "instances", "label": "Instances", "type": "integer"},
    {"key": "resolved", "label": "Resolved", "type": "integer"},
    {"key": "created", "label": "Created", "type": "datetime"},
]


def _kpi(label, value, sub=""):
    return {"label": label, "value": str(value), "sub_string": sub}


class AuroraRunsController(http.Controller):

    @http.route(
        "/api/v1/aurora_ext/runs",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def aurora_ext_runs(self, **kwargs):
        env = request.env
        role_tag = user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Aurora data.",
                status=403,
            )

        Eval = env["aurora.evaluation"].sudo()
        Pipe = env["aurora.pipeline"].sudo()
        Inst = env["aurora.evaluation.instance"].sudo()

        rows = []
        for e in Eval.search([], order="id desc"):
            n = Inst.search_count([("evaluation_id", "=", e.id)])
            resolved = Inst.search_count(
                [("evaluation_id", "=", e.id), ("resolved", "=", True)]
            )
            rows.append({
                "kind": "Evaluation",
                "name": e.name or ("Evaluation #%s" % e.id),
                "stage": e.stage or "",
                "instances": n,
                "resolved": resolved,
                "created": e.create_date.isoformat() if e.create_date else None,
            })
        for p in Pipe.search([], order="id desc"):
            rows.append({
                "kind": "Pipeline",
                "name": p.name or ("Pipeline #%s" % p.id),
                "stage": p.stage or "",
                "instances": 0,
                "resolved": 0,
                "created": p.create_date.isoformat() if p.create_date else None,
            })

        kpis = [
            _kpi("Total Runs", len(rows)),
            _kpi("Evaluations", Eval.search_count([])),
            _kpi("Pipelines", Pipe.search_count([])),
            _kpi("Instances", Inst.search_count([])),
        ]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag,
                "blocks": [
                    {"type": "kpi", "items": kpis},
                    {
                        "type": "table",
                        "title": "Evaluation & pipeline runs",
                        "columns": RUN_COLUMNS,
                        "rows": rows,
                    },
                ],
            },
        )
