import calendar
import logging
from datetime import date, datetime, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _scope, _user_role_tag

_logger = logging.getLogger(__name__)

DEFAULT_BURN_GRAPH_DAYS = 30
MAX_BURN_GRAPH_DAYS = 365

# Sourcing batches collect rows of this backend model via `job_ids`. A
# project "owns" these batches only when its `connected_table` points here.
SOURCING_JOB_MODEL = "video.editor.project"
DELIVERED_BATCH_STATE = "delivered"
APPROVAL_REVIEWED_STATES = ("approved", "rejected")
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


def _budget_domain(project_id, include_inactive):
    domain = []
    if not include_inactive:
        domain.append(("active", "=", True))
    if project_id:
        domain.append(("project_id", "=", project_id))
    return domain


def _cost_line_domain(budgets, filters):
    domain = [("budget_id", "in", budgets.ids)]
    if filters["start"]:
        domain.append(("period", ">=", filters["start"].replace(day=1)))
    if filters["end"]:
        domain.append(("period", "<=", filters["end"]))
    return domain


def _build_budget_kpi(env, project_id, include_inactive):
    Budget = env["etp.project.aws.budget"].sudo()
    budgets = Budget.search(_budget_domain(project_id, include_inactive))

    total_budget = sum(b.budget_amount or 0.0 for b in budgets)
    total_consumed = sum(b.total_consumed or 0.0 for b in budgets)
    total_remaining = sum(b.remaining or 0.0 for b in budgets)
    daily_burn = sum(b.daily_burn_rate or 0.0 for b in budgets)

    runway_days = None
    if daily_burn > 0 and total_remaining > 0:
        runway_days = int(total_remaining // daily_burn)
    elif daily_burn > 0 and total_remaining <= 0:
        runway_days = 0

    return {
        "budget_count": _delivered_batch_count(env, project_id),
        "total_budget": {
            "amount": _round2(total_budget),
            "percentage": 100.0 if total_budget else 0.0,
        },
        "total_consumed": {
            "amount": _round2(total_consumed),
            "percentage": _pct(total_consumed, total_budget),
        },
        "total_remaining": {
            "amount": _round2(total_remaining),
            "percentage": _pct(total_remaining, total_budget),
        },
        "daily_burn_rate": {
            "amount": _round2(daily_burn),
            "percentage": _pct(daily_burn, total_budget),
        },
        "runway_days": runway_days,
    }, budgets


def _build_service_costs(env, budgets, filters):
    if not budgets:
        return {"total_amount": 0.0, "services": []}, 0.0

    Line = env["etp.project.aws.cost.line"].sudo()
    lines = Line.search(_cost_line_domain(budgets, filters))

    totals = {}
    grand_total = 0.0
    for line in lines:
        amount = line.amount_inr or 0.0
        if not amount:
            continue
        grand_total += amount
        key = line.service_name or "Unknown"
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
    Job = env["video.editor.project"].sudo()
    scope = _scope(env)[1]
    job_domain = scope + [("duration_seconds", ">", 0)]
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

    measured = Job.search_count(job_domain)
    rows = Job.read_group(job_domain, fields=["duration_seconds:sum"], groupby=[])
    total_seconds = (rows[0].get("duration_seconds") if rows else 0.0) or 0.0
    total_minutes = total_seconds / 60.0

    avg_rows = Job.read_group(job_domain, fields=["duration_seconds:avg"], groupby=[])
    avg_seconds = (avg_rows[0].get("duration_seconds") if avg_rows else 0.0) or 0.0
    avg_minutes = avg_seconds / 60.0

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


def _build_daily_burn_graph(env, budgets, filters):
    today = date.today()
    end = filters["end"] or today
    if filters["start"]:
        start = filters["start"]
    else:
        start = end - timedelta(days=filters["graph_days"] - 1)

    series = []
    if not budgets:
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

    window_start_dt = datetime.combine(start, datetime.min.time())
    window_end_dt = datetime.combine(end, datetime.max.time())
    daily_by_date = {}
    seen_tables = set()
    for project in budgets.mapped("project_id"):
        table = (getattr(project, "connected_table", None) or "").strip()
        if not table or table in seen_tables:
            continue
        seen_tables.add(table)
        if table not in env:
            continue
        Model = env[table].sudo()
        if not hasattr(Model, "get_daily_burn_timeseries"):
            continue
        rows = Model.get_daily_burn_timeseries(window_start_dt, window_end_dt) or []
        for iso_date, cost in rows:
            try:
                day = date.fromisoformat(iso_date)
            except (TypeError, ValueError):
                continue
            if not (start <= day <= end):
                continue
            amount = float(cost or 0.0)
            if amount <= 0:
                continue
            daily_by_date[day] = daily_by_date.get(day, 0.0) + amount

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


def _project_owns_sourcing_batches(env, project_id):
    """Whether this project's task table is the one sourcing batches collect.

    Batches reference ``video.editor.project`` rows; a project is linked to
    that backend through its ``connected_table``. With no project filter, all
    batches are in scope.
    """
    if not project_id:
        return True
    project = env["project.project"].sudo().browse(project_id)
    table = (getattr(project, "connected_table", None) or "").strip()
    return table == SOURCING_JOB_MODEL


def _delivered_batch_count(env, project_id):
    """Total batches delivered for this project."""
    if not _project_owns_sourcing_batches(env, project_id):
        return 0
    return env["crowley.sourcing.batch.delivery"].sudo().search_count(
        [("state", "=", DELIVERED_BATCH_STATE)]
    )


def _scoped_sourcing_batches(env, project_id):
    if not _project_owns_sourcing_batches(env, project_id):
        return env["crowley.sourcing.batch.delivery"].sudo().browse()
    return env["crowley.sourcing.batch.delivery"].sudo().search(
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


def _serialize_burn_batch(batch, category_labels, review_labels):
    jobs = batch.job_ids
    total_burn = sum(job.llm_qc_cost_usd or 0.0 for job in jobs)
    reviewed = sum(
        1 for job in jobs if job.review_status in APPROVAL_REVIEWED_STATES
    )
    approved = sum(1 for job in jobs if job.review_status == "approved")
    rate = round((approved / reviewed) * 100) if reviewed else 0
    tasks = [
        {
            "ref": job.name or "",
            "category": category_labels.get(job.category, "") if job.category else "",
            "status": review_labels.get(job.review_status, "") if job.review_status else "",
            "burn": _round2(job.llm_qc_cost_usd or 0.0),
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
    Job = env["video.editor.project"].sudo()
    category_labels = dict(Job._fields["category"].selection)
    review_labels = dict(Job._fields["review_status"].selection)
    batches = _scoped_sourcing_batches(env, project_id)
    return {
        "title": "Burn per batch",
        "batches": [
            _serialize_burn_batch(batch, category_labels, review_labels)
            for batch in batches
        ],
    }


class CrowleySourcingBudgetController(http.Controller):

    @http.route(
        "/api/v1/crowley_sourcing_ext/budget/info",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def crowley_sourcing_ext_budget_info(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Crowley Sourcing budget.",
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
            kpi, budgets = _build_budget_kpi(env, project_id, include_inactive)
            service_costs, _service_total = _build_service_costs(env, budgets, filters)
            aht_overview = _build_aht_overview(env, filters)
            burn_graph = _build_daily_burn_graph(env, budgets, filters)
            _last_ts = max((b.last_fetched_at for b in budgets if b.last_fetched_at), default=None) if budgets else None
            last_fetched_at = _last_ts.strftime("%Y-%m-%d %H:%M:%S") if _last_ts else ""
        except Exception as e:
            _logger.exception("crowley_sourcing_ext_budget_info failed")
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
                "budget_timeline": env["etp.project.token.purchase.request"]
                    .sudo()._get_budget_timeline_for_project(
                        project_id,
                        start=filters["start"],
                        end=filters["end"],
                        graph_days=filters["graph_days"],
                    ),
                "burn_per_batch": _build_burn_per_batch(env, project_id),
                "allocation_ledger": env["etp.project.token.purchase.request"]
                    .sudo()._get_allocation_ledger_for_project(project_id),
                "last_fetched_at": last_fetched_at,
            },
        )
