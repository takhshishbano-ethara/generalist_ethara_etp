import json
import logging
from datetime import date, datetime, timedelta

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

DEFAULT_BURN_GRAPH_DAYS = 30
MAX_BURN_GRAPH_DAYS = 365


def _json_response(message="", status=200, data=None, errors=None):
    payload = {
        "status": status,
        "message": message,
        "errors": errors,
        "data": data,
    }
    return Response(
        json.dumps(payload, default=str),
        status=status,
        content_type="application/json",
    )


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
        return None, _json_response(
            message=f"Invalid {label} '{raw}'. Expected YYYY-MM-DD.",
            status=400,
        )


def _parse_positive_int(raw, label, default, maximum):
    if not raw:
        return default, None
    if not str(raw).isdigit():
        return None, _json_response(
            message=f"Invalid {label} '{raw}'. Expected a positive integer.",
            status=400,
        )
    value = int(raw)
    if value <= 0:
        return None, _json_response(
            message=f"{label} must be greater than zero.",
            status=400,
        )
    if value > maximum:
        return None, _json_response(
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
        return None, _json_response(
            message=f"Invalid {label} '{raw}'. Expected a number.",
            status=400,
        )
    if value <= 0:
        return None, _json_response(
            message=f"{label} must be greater than zero.",
            status=400,
        )
    return value, None


def _resolve_project_id(env, params):
    raw = (params.get("project_id") or "").strip()
    if not raw:
        return None, None
    if not raw.isdigit():
        return None, _json_response(
            message=f"Invalid project_id '{raw}'. Expected an integer.",
            status=400,
        )
    project_id = int(raw)
    if not env["project.project"].sudo().browse(project_id).exists():
        return None, _json_response(
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
        return None, _json_response(
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


def _compute_daily_burn(env, budgets, days=7):
    if not budgets:
        return 0.0
    cutoff = date.today() - timedelta(days=days)
    rows = env["etp.project.aws.cost.line"].sudo().read_group(
        [
            ("budget_id", "in", budgets.ids),
            ("granularity", "=", "day"),
            ("is_model_breakdown", "=", False),
            ("period", ">=", cutoff),
        ],
        fields=["amount_source:sum"],
        groupby=[],
    )
    total = (rows[0].get("amount_source") if rows else 0.0) or 0.0
    return total / days


def _build_budget_kpi(env, project_id, include_inactive):
    Budget = env["etp.project.aws.budget"].sudo()
    budgets = Budget.search(_budget_domain(project_id, include_inactive))

    total_budget = sum(b.budget_amount or 0.0 for b in budgets)
    total_consumed = sum(b.consumed_amount or 0.0 for b in budgets)
    total_remaining = sum(b.remaining_amount or 0.0 for b in budgets)
    daily_burn = _compute_daily_burn(env, budgets)

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
        return {"total_amount": 0.0, "services": []}

    Line = env["etp.project.aws.cost.line"].sudo()
    lines = Line.search(_cost_line_domain(budgets, filters))

    totals = {}
    grand_total = 0.0
    for line in lines:
        amount = line.amount_source or 0.0
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
    }


def _empty_aht_overview(target_aht):
    return {
        "aht_measured_count": 0,
        "aht_total_minutes": 0.0,
        "aht_average_minutes": 0.0,
        "target_aht_minutes": _round2(target_aht) if target_aht else None,
        "target_indicator": "no_target" if not target_aht else "no_data",
    }


def _empty_daily_burn_graph(filters):
    today = date.today()
    end = filters["end"] or today
    start = filters["start"] or (end - timedelta(days=filters["graph_days"] - 1))
    series = []
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


def _max_last_fetched_at(budgets):
    if not budgets:
        return ""
    ts = max((b.last_fetched_at for b in budgets if b.last_fetched_at), default=None)
    return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def _build_budget_info_payload(env, project_id, include_inactive, filters):
    kpi, budgets = _build_budget_kpi(env, project_id, include_inactive)
    service_costs = _build_service_costs(env, budgets, filters)

    TPR = env["etp.project.token.purchase.request"].sudo()
    budget_timeline = TPR._get_budget_timeline_for_project(
        project_id,
        start=filters["start"],
        end=filters["end"],
        graph_days=filters["graph_days"],
    )
    allocation_ledger = TPR._get_allocation_ledger_for_project(project_id)

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
        "aht_overview": _empty_aht_overview(filters["target_aht"]),
        "daily_burn_graph": _empty_daily_burn_graph(filters),
        "budget_timeline": budget_timeline,
        "burn_per_batch": {
            "title": "Burn per batch",
            "batches": [],
        },
        "allocation_ledger": allocation_ledger,
        "last_fetched_at": _max_last_fetched_at(budgets),
    }


class KenseiBudgetController(http.Controller):

    @http.route(
        "/api/v1/kensei_ext/budget/info",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def kensei_ext_budget_info(self, **kwargs):
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
            data = _build_budget_info_payload(env, project_id, include_inactive, filters)
        except Exception as e:
            _logger.exception("kensei_ext_budget_info failed")
            return _json_response(
                message="Failed to build budget info.",
                status=400,
                errors=[str(e)],
            )

        return _json_response(message="OK", status=200, data=data)
