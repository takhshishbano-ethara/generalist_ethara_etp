import logging
from datetime import date, datetime, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    _budget_usd,
    _scope,
    _task_cost,
    _user_role_tag,
)

_logger = logging.getLogger(__name__)

DEFAULT_BURN_GRAPH_DAYS = 30
MAX_BURN_GRAPH_DAYS = 365

FENRIR_JOB_MODEL = "fenrir.task"
DELIVERED_BATCH_STATE = "delivered"
BURN_BATCH_LIMIT = 200
BURN_TASK_LIMIT = 200
APPROVED_STATUSES = ("completed", "approved")
REVIEWED_STATUSES = ("completed", "approved", "rejected", "cancelled")


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
    project_id = int(raw)
    if not env["project.project"].sudo().browse(project_id).exists():
        return None, return_Response(
            message=f"Project '{project_id}' not found.",
            status=404,
        )
    return project_id, None


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


def _scope_tasks(env, project_id):
    _tag, role_domain, _all_tasks = _scope(env)
    return env["fenrir.task"].sudo().search(role_domain)


def _project_owns_fenrir_batches(env, project_id):
    if not project_id:
        return True
    project = env["project.project"].sudo().browse(project_id)
    table = (getattr(project, "connected_table", None) or "").strip()
    return table == FENRIR_JOB_MODEL


def _delivered_batch_count(env, project_id):
    if not _project_owns_fenrir_batches(env, project_id):
        return 0
    return env["fenrir.batch.delivery"].sudo().search_count(
        [("state", "=", DELIVERED_BATCH_STATE)]
    )


def _scoped_fenrir_batches(env, project_id):
    if not _project_owns_fenrir_batches(env, project_id):
        return env["fenrir.batch.delivery"].sudo().browse()
    return env["fenrir.batch.delivery"].sudo().search([], limit=BURN_BATCH_LIMIT)


def _approval_status(rate, reviewed):
    if not reviewed:
        return "pending"
    if rate >= 80:
        return "approved"
    if rate >= 50:
        return "partial"
    return "rejected"


def _serialize_burn_batch(batch, status_labels):
    jobs = batch.job_ids
    total_burn = sum(_task_cost(job) for job in jobs)
    reviewed = sum(1 for job in jobs if job.status in REVIEWED_STATUSES)
    approved = sum(1 for job in jobs if job.status in APPROVED_STATUSES)
    rate = round((approved / reviewed) * 100) if reviewed else 0
    tasks = [
        {
            "ref": job.code or "",
            "category": job.category_id.name if job.category_id else "",
            "status": status_labels.get(job.status, "") if job.status else "",
            "burn": _round2(_task_cost(job)),
        }
        for job in jobs[:BURN_TASK_LIMIT]
    ]
    return {
        "batch_id": batch.name or "",
        "videos": batch.job_count,
        "burn": _round2(total_burn),
        "approval": {"rate": rate, "status": _approval_status(rate, reviewed)},
        "feedback": batch.notes or "",
        "tasks": tasks,
    }


def _build_burn_per_batch(env, project_id):
    Task = env["fenrir.task"].sudo()
    status_labels = dict(Task._fields["status"].selection)
    batches = _scoped_fenrir_batches(env, project_id)
    return {
        "title": "Burn per batch",
        "batches": [_serialize_burn_batch(b, status_labels) for b in batches],
    }


def _build_budget_kpi(env, project_id):
    """Synthesize a single 'budget' from the system cap vs accepted-offer spend."""
    tasks = _scope_tasks(env, project_id)

    cap = _budget_usd(env)
    total_consumed = sum(_task_cost(t) for t in tasks)
    total_remaining = max(cap - total_consumed, 0.0)
    daily_burn = total_consumed / DEFAULT_BURN_GRAPH_DAYS if total_consumed else 0.0

    runway_days = None
    if daily_burn > 0 and total_remaining > 0:
        runway_days = int(total_remaining // daily_burn)
    elif daily_burn > 0 and total_remaining <= 0:
        runway_days = 0

    return {
        "budget_count": _delivered_batch_count(env, project_id),
        "total_budget": {
            "amount": _round2(cap),
            "percentage": 100.0 if cap else 0.0,
        },
        "total_consumed": {
            "amount": _round2(total_consumed),
            "percentage": _pct(total_consumed, cap),
        },
        "total_remaining": {
            "amount": _round2(total_remaining),
            "percentage": _pct(total_remaining, cap),
        },
        "daily_burn_rate": {
            "amount": _round2(daily_burn),
            "percentage": _pct(daily_burn, cap),
        },
        "runway_days": runway_days,
    }, tasks


def _build_service_costs(env, tasks, filters):
    """Break spend down by category — fenrir's stand-in for AWS services."""
    if not tasks:
        return {"total_amount": 0.0, "services": []}, 0.0

    totals = {}
    grand_total = 0.0
    for task in tasks:
        amount = _task_cost(task)
        if not amount:
            continue
        grand_total += amount
        key = task.category_id.name or "Uncategorized"
        totals[key] = totals.get(key, 0.0) + amount

    breakdown = [
        {
            "service_name": name,
            "amount": _round2(amount),
            "percentage": _pct(amount, grand_total),
        }
        for name, amount in sorted(totals.items(), key=lambda item: -item[1])
    ]
    return {
        "total_amount": _round2(grand_total),
        "services": breakdown,
    }, grand_total


def _build_aht_overview(env, filters):
    """Average handling time, derived from estimated_completion_time_hours."""
    Task = env["fenrir.task"].sudo()
    _tag, scope, _t = _scope(env)
    job_domain = scope + [("estimated_completion_time_hours", ">", 0)]
    if filters["start"]:
        job_domain.append((
            "create_date",
            ">=",
            datetime.combine(filters["start"], datetime.min.time()),
        ))
    if filters["end"]:
        job_domain.append((
            "create_date",
            "<",
            datetime.combine(filters["end"], datetime.min.time()) + timedelta(days=1),
        ))

    measured = Task.search_count(job_domain)
    rows = Task.read_group(
        job_domain, fields=["estimated_completion_time_hours:sum"], groupby=[]
    )
    total_hours = (rows[0].get("estimated_completion_time_hours") if rows else 0.0) or 0.0
    total_minutes = total_hours * 60.0

    avg_rows = Task.read_group(
        job_domain, fields=["estimated_completion_time_hours:avg"], groupby=[]
    )
    avg_hours = (avg_rows[0].get("estimated_completion_time_hours") if avg_rows else 0.0) or 0.0
    avg_minutes = avg_hours * 60.0

    target = filters["target_aht"]
    target_indicator = "no_target"
    if target:
        if not measured:
            target_indicator = "no_data"
        elif avg_minutes <= target:
            target_indicator = "on_target"
        else:
            target_indicator = "above_target"

    return {
        "aht_measured_count": measured,
        "aht_total_minutes": _round2(total_minutes),
        "aht_average_minutes": _round2(avg_minutes),
        "target_aht_minutes": _round2(target) if target else None,
        "target_indicator": target_indicator,
    }


def _build_daily_burn_graph(env, tasks, filters):
    today = date.today()
    end = filters["end"] or today
    if filters["start"]:
        start = filters["start"]
    else:
        start = end - timedelta(days=filters["graph_days"] - 1)

    series = []
    if not tasks:
        cursor = start
        while cursor <= end:
            series.append({"date": cursor.isoformat(), "amount": 0.0})
            cursor += timedelta(days=1)
        return {
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "total_amount": 0.0,
            "average_per_day": 0.0,
            "peak_day": None,
            "series": series,
        }

    daily_by_date = {}
    for task in tasks:
        day_field = task.write_date
        if not day_field:
            continue
        day = day_field.date() if isinstance(day_field, datetime) else day_field
        if start <= day <= end:
            daily_by_date[day] = daily_by_date.get(day, 0.0) + _task_cost(task)

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


class FenrirBudgetController(http.Controller):

    @http.route(
        "/api/v1/fenrir_ext/budget/info",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def fenrir_ext_budget_info(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Fenrir budget.",
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
            kpi, tasks = _build_budget_kpi(env, project_id)
            service_costs, _service_total = _build_service_costs(env, tasks, filters)
            aht_overview = _build_aht_overview(env, filters)
            burn_graph = _build_daily_burn_graph(env, tasks, filters)
        except Exception as e:
            _logger.exception("fenrir_ext_budget_info failed")
            return return_Response(
                message="Failed to build budget info.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="OK",
            status=200,
            data={
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
            },
        )
