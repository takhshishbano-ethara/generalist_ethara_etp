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
        "budget_count": 0,
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
        "burn_per_batch": {
            "title": "Burn per batch",
            "batches": [],
        },
        "allocation_ledger": [],
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
