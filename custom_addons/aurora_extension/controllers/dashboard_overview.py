"""Aurora overview tab."""

import logging
from datetime import datetime

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    COMPLETED_STATES,
    IN_PROGRESS_STATES,
    initials,
    kpi_item,
    pct,
    resolve_dashboard_filters,
    create_date_domain,
    time_ago,
    user_role_tag,
)

RECENT_ACTIVITY_LIMIT = 8

# "Stage Funnel" for Aurora instances, mirroring crowley's task_progress shape:
# Pending → In Progress (building/built/running) → Resolved.
STAGE_FUNNEL = (
    ("pending", "Pending", ("pending",)),
    ("in_progress", "In Progress", IN_PROGRESS_STATES[1:]),  # building/built/running
    ("resolved", "Resolved", COMPLETED_STATES),
)


def _compute_kpi(env, scope):
    Instance = env["aurora.evaluation.instance"].sudo()
    Evaluation = env["aurora.evaluation"].sudo()

    total = Instance.search_count(scope)
    resolved = Instance.search_count(scope + [("status", "in", list(COMPLETED_STATES))])
    errored = Instance.search_count(scope + [("status", "=", "error")])
    in_progress = Instance.search_count(
        scope + [("status", "in", list(IN_PROGRESS_STATES))]
    )
    # Distinct org/repo pairs. _read_group returns one group per (org, repo)
    # pair, so the length matches the deprecated read_group(..., lazy=False)
    # result exactly — pure forward-compat swap, no output change.
    repos_covered = len(Instance._read_group(scope, groupby=["org", "repo"]))
    eval_runs = Evaluation.search_count([])

    items = [
        kpi_item(
            "total_instances", "Total Instances", total,
            sub_string=f"{eval_runs} evaluation run(s)",
        ),
        kpi_item(
            "resolved", "Resolved", f"{resolved}/{total}",
            sub_string=f"{pct(resolved, total)}% resolve rate",
        ),
        kpi_item(
            "in_progress", "In Progress", in_progress,
            sub_string=f"{errored} errored",
        ),
        kpi_item(
            "repos_covered", "Repos Covered", repos_covered,
            sub_string="distinct org/repo pairs",
        ),
    ]
    return {"count": len(items), "items": items}


def _compute_task_progress(env, scope):
    Instance = env["aurora.evaluation.instance"].sudo()
    total = Instance.search_count(scope)
    items = []
    done_count = 0
    for key, label, states in STAGE_FUNNEL:
        count = Instance.search_count(scope + [("status", "in", list(states))])
        if key == "resolved":
            done_count = count
        items.append({
            "key": key,
            "label": label,
            "value": count,
            "percentage": pct(count, total),
        })
    rejected_rework = Instance.search_count(
        scope + [("status", "in", ("unresolved", "error"))]
    )
    return {
        "label": "Stage Funnel",
        "total": total,
        "count": len(items),
        "items": items,
        "conversion_pct": pct(done_count, total),
        "rejected_rework": rejected_rework,
    }


def _compute_recent_activity(env, scope):
    Instance = env["aurora.evaluation.instance"].sudo()
    records = Instance.search(
        scope, order="write_date desc, id desc", limit=RECENT_ACTIVITY_LIMIT
    )
    items = []
    for rec in records:
        status = rec.status
        if status in COMPLETED_STATES:
            action = "resolved"
        elif status == "unresolved":
            action = "unresolved"
        elif status == "error":
            action = "failed"
        elif status in IN_PROGRESS_STATES:
            action = "processing"
        else:
            action = "updated"
        actor = rec.create_uid
        when = rec.write_date
        items.append({
            "actor_id": actor.id,
            "actor_name": actor.name or "",
            "actor_initials": initials(actor.name),
            "action": action,
            "task_code": rec.instance_id or rec.display_name or "",
            "timestamp": when.isoformat() if when else "",
            "time_ago": time_ago(when),
        })
    return {"label": "Recent Activity", "count": str(len(items)), "items": items}


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

        params = request.params or {}
        filters, error = resolve_dashboard_filters(params)
        if error is not None:
            return error

        # Aurora is not row-scoped (no taskers); all Aurora users see every
        # instance. Date filter only.
        scope = create_date_domain(filters["start"], filters["end"])

        # Single `overview` wrapper — every section key is always present
        # (crowley_sourcing parity). Sections Aurora has no source for
        # (budget / burn_rate / accepted_per_day) and the crowley-only chart
        # sections are returned blank ({}).
        try:
            overview = {
                "role": role_tag,
                "kpi": _compute_kpi(env, scope),
                "budget": {},
                "burn_rate": {},
                "accepted_per_day": {},
                "task_progress": _compute_task_progress(env, scope),
                "approved_per_week": {},
                "recent_activity": _compute_recent_activity(env, scope),
                "coordination_events": {},
                "tasks_done_chart": {},
                "burned_amount_chart": {},
                "my_activity": {},
            }
        except Exception:
            # Full traceback to the server log; the client gets a clean JSON
            # envelope (status 400 — the status return_Response formats with a
            # status_code) with no internal detail leaked.
            _logger.exception("aurora_ext_dashboard_overview failed")
            return return_Response(
                message="Failed to build Aurora overview.",
                status=400,
            )

        return return_Response(
            message="OK",
            status=200,
            data={"overview": overview},
        )
