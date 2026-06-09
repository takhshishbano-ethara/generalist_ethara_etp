import logging
from datetime import date, datetime, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .main import _job_scope_domain, _require_leviathan_user

_logger = logging.getLogger(__name__)

QC_USE_CASE_KEYWORDS = ("qc", "quality")
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


def _build_model_costs(env, filters):
    Usage = env["aws.cost.ai.usage"].sudo()
    domain = []
    if filters["start"]:
        domain.append(("period", ">=", filters["start"]))
    if filters["end"]:
        domain.append(("period", "<=", filters["end"]))
    rows = Usage.search(domain)

    totals = {}
    qc_total = 0.0
    grand_total = 0.0
    for row in rows:
        amount = row.total_cost_inr or 0.0
        if not amount:
            continue
        grand_total += amount
        key = row.model_name or "Unknown"
        totals[key] = totals.get(key, 0.0) + amount
        use_case = (row.use_case or "").lower()
        if any(token in use_case for token in QC_USE_CASE_KEYWORDS):
            qc_total += amount

    breakdown = [
        {
            "model_name": name,
            "amount": _round2(amount),
            "percentage": _pct(amount, grand_total),
        }
        for name, amount in sorted(totals.items(), key=lambda item: -item[1])
    ]
    return {
        "total_amount": _round2(grand_total),
        "models": breakdown,
    }, grand_total, qc_total


def _build_qc_spend(env, filters, llm_total, qc_total):
    Job = env["leviathan.job"].sudo()
    scope = _job_scope_domain()
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
    total_hours = total_seconds / 3600.0

    avg_rows = Job.read_group(job_domain, fields=["duration_seconds:avg"], groupby=[])
    avg_seconds = (avg_rows[0].get("duration_seconds") if avg_rows else 0.0) or 0.0
    avg_minutes = avg_seconds / 60.0

    cost_per_aht_hour = (qc_total / total_hours) if total_hours else 0.0

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
        "qc_spend_amount": _round2(qc_total),
        "qc_spend_percentage": _pct(qc_total, llm_total),
        "aht_measured_count": measured,
        "aht_total_minutes": _round2(total_minutes),
        "aht_average_minutes": _round2(avg_minutes),
        "qc_cost_per_aht_hour": _round2(cost_per_aht_hour),
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

    Daily = env["ai.cost.daily"].sudo()
    rows = Daily.read_group(
        domain=[("date", ">=", start), ("date", "<=", end)],
        fields=["amount_inr:sum"],
        groupby=["date:day"],
        lazy=False,
    )
    by_date = {}
    for row in rows:
        raw = row.get("date:day")
        if not raw:
            continue
        try:
            parsed = datetime.strptime(raw, "%d %b %Y").date()
        except ValueError:
            continue
        by_date[parsed] = (row.get("amount_inr") or 0.0)

    series = []
    cursor = start
    while cursor <= end:
        amount = by_date.get(cursor, 0.0)
        series.append({"date": cursor.isoformat(), "amount": _round2(amount)})
        cursor += timedelta(days=1)

    total = sum(point["amount"] for point in series)
    peak = max(series, key=lambda p: p["amount"]) if series else None
    average = total / len(series) if series else 0.0
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "total_amount": _round2(total),
        "average_per_day": _round2(average),
        "peak_day": peak,
        "series": series,
    }


class LeviathanBudgetController(http.Controller):

    @http.route(
        "/api/v1/leviathan_ext/budget/info",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def leviathan_ext_budget_info(self, **kwargs):
        guard = _require_leviathan_user()
        if guard is not None:
            return guard

        env = request.env
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
            kpi, _budgets = _build_budget_kpi(env, project_id, include_inactive)
            model_costs, llm_total, qc_total = _build_model_costs(env, filters)
            qc_spend = _build_qc_spend(env, filters, llm_total, qc_total)
            burn_graph = _build_daily_burn_graph(env, filters)
        except Exception as e:
            _logger.exception("leviathan_ext_budget_info failed")
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
                "model_costs": model_costs,
                "qc_spend": qc_spend,
                "daily_burn_graph": burn_graph,
            },
        )
