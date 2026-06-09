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
        "budget_count": len(budgets),
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
    Job = env["crowley.generation"].sudo()
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

    Line = env["etp.project.aws.cost.line"].sudo()
    window_start_month = start.replace(day=1)
    window_end_month = end.replace(day=1)
    lines = Line.search([
        ("budget_id", "in", budgets.ids),
        ("period", ">=", window_start_month),
        ("period", "<=", window_end_month),
    ])

    monthly_totals = {}
    for line in lines:
        amount = line.amount_inr or 0.0
        if not amount or not line.period:
            continue
        month_key = line.period.replace(day=1)
        monthly_totals[month_key] = monthly_totals.get(month_key, 0.0) + amount

    daily_by_date = {}
    for month_start, month_total in monthly_totals.items():
        days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
        per_day = month_total / days_in_month if days_in_month else 0.0
        for offset in range(days_in_month):
            day = month_start + timedelta(days=offset)
            if start <= day <= end:
                daily_by_date[day] = daily_by_date.get(day, 0.0) + per_day

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


class CrowleyBudgetController(http.Controller):

    @http.route(
        "/api/v1/crowley_ext/budget/info",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def crowley_ext_budget_info(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Crowley budget.",
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
        except Exception as e:
            _logger.exception("crowley_ext_budget_info failed")
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
                "budget_timeline": {
                    "title": "Budget Added & Consumption Over Time",
                    "range": "7d",
                    "available_now": 7550,
                    "y_axis": {"min": 0, "max": 20000, "step": 5000},
                    "window": {"start": "2026-05-14", "end": "2026-06-09"},
                    "series": [
                        {
                            "date": "2026-05-14",
                            "available_balance": 10000,
                            "consumed_to_date": 0,
                            "added_to_date": 10000,
                            "event": {}
                        },
                        {
                            "date": "2026-05-20",
                            "available_balance": 19500,
                            "consumed_to_date": 1200,
                            "added_to_date": 20000,
                            "event": {
                                "type": "top_up",
                                "label": "Top-up",
                                "added": 5000,
                                "available_after": 19500,
                                "spent_since_last_topup": 1200
                            }
                        }
                    ]
                },
                "burn_per_batch": {
                    "title": "Burn per batch",
                    "batches": [
                        {
                            "batch_id": "B-023",
                            "videos": 640,
                            "burn": 3380,
                            "approval": {
                                "rate": 86,
                                "status": "approved"
                            },
                            "feedback": "Everything was good",
                            "tasks": [
                                {
                                    "ref": "CRW000211",
                                    "category": "Human Activities",
                                    "status": "Revision",
                                    "burn": 3.70
                                }
                            ]
                        }
                    ]
                },
                "allocation_ledger": {
                    "title": "Allocation Ledger",
                    "entries": [
                        {
                            "datetime": "2026-05-20T09:30:00Z",
                            "action": "top_up",
                            "action_label": "Top-up",
                            "amount": 5000,
                            "balance_before": 8600
                        },
                        {
                            "datetime": "2026-05-14T11:15:00Z",
                            "action": "set_initial",
                            "action_label": "Set initial",
                            "amount": 10000,
                            "balance_before": ""
                        }
                    ]
                }

            },
        )
