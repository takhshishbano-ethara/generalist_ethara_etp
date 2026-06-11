import logging
from datetime import date, datetime, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _scope, _user_role_tag, _total_tokens

_logger = logging.getLogger(__name__)

DEFAULT_BURN_GRAPH_DAYS = 30
MAX_BURN_GRAPH_DAYS = 365

# Burn-per-batch batches collect rows of this backend model via `job_ids`. A
# project "owns" these batches only when its `connected_table` points here.
TALOS_JOB_MODEL = "talos.talos"
DELIVERED_BATCH_STATE = "delivered"
# QC outcome: passed counts as approved, failed as rework; a task is
# "reviewed" once it has either verdict (pending excluded). Burn = QC tokens.
APPROVED_VERDICTS = ("passed",)
REWORK_VERDICTS = ("failed",)
REVIEWED_VERDICTS = APPROVED_VERDICTS + REWORK_VERDICTS
BURN_BATCH_LIMIT = 200
BURN_TASK_LIMIT = 200


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def _round2(value):
    return round(float(value or 0.0), 2)


def _parse_date(raw, label):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, return_Response(
            message=f"Invalid {label} '{raw}'. Expected YYYY-MM-DD.",
            status=400,
        )


def _parse_positive_int(raw, label, default, maximum):
    if not raw:
        return default, None
    if not str(raw).isdigit():
        return None, return_Response(
            message=f"Invalid {label} '{raw}'. Expected a positive integer.",
            status=400,
        )
    value = int(raw)
    if value <= 0:
        return None, return_Response(
            message=f"{label} must be greater than zero.",
            status=400,
        )
    if value > maximum:
        return None, return_Response(
            message=f"{label} must be <= {maximum}.",
            status=400,
        )
    return value, None


def _parse_positive_float(raw, label):
    if not raw:
        return None, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, return_Response(
            message=f"Invalid {label} '{raw}'. Expected a number.",
            status=400,
        )
    if value <= 0:
        return None, return_Response(
            message=f"{label} must be greater than zero.",
            status=400,
        )
    return value, None


def _resolve_project_id(env, params):
    raw = (params.get("project_id") or "").strip()
    if not raw:
        return None, None
    if not raw.isdigit():
        return None, return_Response(
            message=f"Invalid project_id '{raw}'. Expected an integer.",
            status=400,
        )
    return int(raw), None


def _resolve_filters(params):
    raw_start = (params.get("start_date") or "").strip()
    raw_end = (params.get("end_date") or "").strip()
    start = end = None
    if raw_start:
        start, error = _parse_date(raw_start, "start_date")
        if error is not None:
            return None, error
    if raw_end:
        end, error = _parse_date(raw_end, "end_date")
        if error is not None:
            return None, error
    if start and end and start > end:
        return None, return_Response(
            message="Invalid date range: start_date must be on or before end_date.",
            status=400,
        )

    days, error = _parse_positive_int(
        (params.get("graph_days") or "").strip(),
        "graph_days",
        DEFAULT_BURN_GRAPH_DAYS,
        MAX_BURN_GRAPH_DAYS,
    )
    if error is not None:
        return None, error

    target_aht, error = _parse_positive_float(
        (params.get("target_aht_minutes") or "").strip(),
        "target_aht_minutes",
    )
    if error is not None:
        return None, error

    return {
        "start": start,
        "end": end,
        "graph_days": days,
        "target_aht": target_aht,
    }, None


def _build_budget_kpi(env, project_id, include_inactive):
    return {
        "budget_count": _delivered_batch_count(env, project_id),
        "total_budget": {"amount": 0.0, "percentage": 0.0},
        "total_consumed": {"amount": 0.0, "percentage": 0.0},
        "total_remaining": {"amount": 0.0, "percentage": 0.0},
        "daily_burn_rate": {"amount": 0.0, "percentage": 0.0},
        "runway_days": None,
    }


def _build_service_costs(env, filters):
    return {"total_amount": 0.0, "services": []}


def _build_aht_overview(env, filters):
    target = filters["target_aht"]
    target_indicator = "no_target" if not target else "no_data"
    return {
        "aht_measured_count": 0,
        "aht_total_minutes": 0.0,
        "aht_average_minutes": 0.0,
        "target_aht_minutes": _round2(target) if target else None,
        "target_indicator": target_indicator,
    }


def _build_daily_burn_graph(env, filters):
    today = date.today()
    end = filters["end"] or today
    if filters["start"]:
        start = filters["start"]
    else:
        start = end - timedelta(days=filters["graph_days"] - 1)

    scope = _scope(env)[1]
    window_start_dt = datetime.combine(start, datetime.min.time())
    window_end_dt = datetime.combine(end, datetime.max.time())

    records = env["talos.talos"].sudo().search(
        scope
        + [
            ("create_date", ">=", window_start_dt),
            ("create_date", "<=", window_end_dt),
        ]
    )
    daily_by_date = {}
    for rec in records:
        when = rec.create_date
        if not when:
            continue
        day = when.date()
        if not (start <= day <= end):
            continue
        amount = float(_total_tokens(rec))
        if amount <= 0:
            continue
        daily_by_date[day] = daily_by_date.get(day, 0.0) + amount

    series = []
    cursor = start
    while cursor <= end:
        amount = daily_by_date.get(cursor, 0.0)
        series.append({"date": cursor.isoformat(), "amount": _round2(amount)})
        cursor += timedelta(days=1)

    total = sum(point["amount"] for point in series)
    peak = max(series, key=lambda p: p["amount"]) if series else None
    if peak and peak["amount"] == 0.0:
        peak = None
    average = total / len(series) if series else 0.0
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "total_amount": _round2(total),
        "average_per_day": _round2(average),
        "peak_day": peak,
        "series": series,
    }


def _build_budget_info_payload(env, project_id, include_inactive, filters):
    kpi = _build_budget_kpi(env, project_id, include_inactive)
    service_costs = _build_service_costs(env, filters)
    aht_overview = _build_aht_overview(env, filters)
    burn_graph = _build_daily_burn_graph(env, filters)

    return {
        "filters": {
            "project_id": project_id,
            "include_inactive": include_inactive,
            "start_date": filters["start"].isoformat() if filters["start"] else None,
            "end_date": filters["end"].isoformat() if filters["end"] else None,
            "graph_days": filters["graph_days"],
            "target_aht_minutes": (
                _round2(filters["target_aht"]) if filters["target_aht"] else None
            ),
        },
        "kpi": kpi,
        "service_costs": service_costs,
        "aht_overview": aht_overview,
        "daily_burn_graph": burn_graph,
        "budget_timeline": [],
        "burn_per_batch": _build_burn_per_batch(env, project_id),
        "allocation_ledger": [],
    }


def _project_owns_batches(env, project_id):
    """Whether this project's task table is the one burn batches collect.

    Batches reference ``talos.talos`` rows; a project is linked to that
    backend through its ``connected_table``. With no project filter, all
    batches are in scope.
    """
    if not project_id:
        return True
    project = env["project.project"].sudo().browse(project_id)
    table = (getattr(project, "connected_table", None) or "").strip()
    return table == TALOS_JOB_MODEL


def _delivered_batch_count(env, project_id):
    """Total batches delivered for this project."""
    if not _project_owns_batches(env, project_id):
        return 0
    return env["talos.batch.delivery"].sudo().search_count(
        [("state", "=", DELIVERED_BATCH_STATE)]
    )


def _scoped_batches(env, project_id):
    if not _project_owns_batches(env, project_id):
        return env["talos.batch.delivery"].sudo().browse()
    return env["talos.batch.delivery"].sudo().search(
        [], limit=BURN_BATCH_LIMIT
    )


def _approval_status(rate, reviewed):
    if not reviewed:
        return "pending"
    if rate >= 80:
        return "approved"
    if rate >= 50:
        return "partial"
    return "rejected"


def _serialize_burn_batch(batch, type_labels, status_labels):
    tasks_recs = batch.job_ids
    total_burn = sum(int(_total_tokens(task)) for task in tasks_recs)
    reviewed = sum(1 for task in tasks_recs if task.qc_status in REVIEWED_VERDICTS)
    approved = sum(1 for task in tasks_recs if task.qc_status in APPROVED_VERDICTS)
    rate = round((approved / reviewed) * 100) if reviewed else 0
    tasks = [
        {
            "ref": task.task_id or "",
            "category": type_labels.get(task.task_type, "") if task.task_type else "",
            "status": status_labels.get(task.qc_status, "") if task.qc_status else "",
            "burn": int(_total_tokens(task)),
        }
        for task in tasks_recs[:BURN_TASK_LIMIT]
    ]
    return {
        "batch_id": batch.name or "",
        "videos": batch.job_count,
        "burn": total_burn,
        "approval": {"rate": rate, "status": _approval_status(rate, reviewed)},
        "feedback": batch.notes or "",
        "tasks": tasks,
    }


def _build_burn_per_batch(env, project_id):
    Task = env["talos.talos"].sudo()
    type_labels = dict(Task._fields["task_type"].selection)
    status_labels = dict(Task._fields["qc_status"].selection)
    batches = _scoped_batches(env, project_id)
    return {
        "title": "Burn per batch",
        "batches": [
            _serialize_burn_batch(batch, type_labels, status_labels)
            for batch in batches
        ],
    }


class TalosBudgetController(http.Controller):

    @http.route(
        "/api/v1/talos_ext/budget/info",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def talos_ext_budget_info(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Talos budget.",
                status=403,
            )

        params = request.params or {}

        project_id, error = _resolve_project_id(env, params)
        if error is not None:
            return error

        filters, error = _resolve_filters(params)
        if error is not None:
            return error

        include_inactive = (params.get("include_inactive") or "").strip().lower() in (
            "1", "true", "yes",
        )

        try:
            data = _build_budget_info_payload(env, project_id, include_inactive, filters)
        except Exception as e:
            _logger.exception("talos_ext_budget_info failed")
            return return_Response(
                message="Failed to build budget info.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(message="OK", status=200, data=data)

    @http.route(
        "/api/v1/talos_ext/budget/fetch",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def talos_ext_budget_fetch(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Talos budget.",
                status=403,
            )

        params = request.params or {}

        project_id, error = _resolve_project_id(env, params)
        if error is not None:
            return error

        filters, error = _resolve_filters(params)
        if error is not None:
            return error

        include_inactive = (params.get("include_inactive") or "").strip().lower() in (
            "1", "true", "yes",
        )

        try:
            data = _build_budget_info_payload(env, project_id, include_inactive, filters)
        except Exception as e:
            _logger.exception("talos_ext_budget_fetch failed")
            return return_Response(
                message="Failed to build budget info after fetch.",
                status=400,
                errors=[str(e)],
            )

        data["fetch_summary"] = {
            "budgets_total": 0,
            "budgets_fetched": 0,
            "errors": [],
        }

        return return_Response(
            message="OK",
            status=200,
            data=data,
        )
