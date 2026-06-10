from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, time, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    UNCATEGORIZED_LABEL,
    _money,
    _parse_date,
    _pct1,
    _scope,
    _user_role_tag,
)

_logger = logging.getLogger(__name__)

DEFAULT_GRAPH_DAYS = 30
MIN_GRAPH_DAYS = 1
MAX_GRAPH_DAYS = 365
DEFAULT_TARGET_AHT_MINUTES = 0.0


def _resolve_filters(params):
    start = _parse_date(params.get("start_date"))
    end = _parse_date(params.get("end_date"))
    try:
        graph_days = int(params.get("graph_days") or DEFAULT_GRAPH_DAYS)
    except (TypeError, ValueError):
        graph_days = DEFAULT_GRAPH_DAYS
    graph_days = max(MIN_GRAPH_DAYS, min(graph_days, MAX_GRAPH_DAYS))
    try:
        target_aht = float(params.get("target_aht_minutes") or DEFAULT_TARGET_AHT_MINUTES)
    except (TypeError, ValueError):
        target_aht = DEFAULT_TARGET_AHT_MINUTES
    include_inactive = str(params.get("include_inactive") or "").lower() in {"1", "true", "yes"}
    try:
        project_id_raw = params.get("project_id")
        project_id = int(project_id_raw) if project_id_raw not in (None, "") else None
    except (TypeError, ValueError):
        project_id = None
    return {
        "project_id": project_id,
        "include_inactive": include_inactive,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "graph_days": graph_days,
        "target_aht_minutes": target_aht,
    }


def _budget_amount(env):
    try:
        raw = env["ir.config_parameter"].sudo().get_param("skoll.budget_usd")
        return float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return 0.0


def _gen_date_domain(filters):
    dom = []
    start = _parse_date(filters.get("start_date"))
    end = _parse_date(filters.get("end_date"))
    if start:
        dom.append(("create_date", ">=", datetime.combine(start, time.min)))
    if end:
        dom.append(("create_date", "<=", datetime.combine(end, time.max)))
    return dom


def _gen_scope(env, filters):
    _tag, task_domain, _personas = _scope(env)
    out = []
    for leaf in task_domain:
        if isinstance(leaf, (list, tuple)) and len(leaf) == 3:
            f, op, v = leaf
            out.append((f"task_id.{f}", op, v))
        else:
            out.append(leaf)
    out += _gen_date_domain(filters)
    return out


def _build_kpi(env, gen_scope, filters):
    Generation = env["skoll.generation"].sudo()
    budget = _budget_amount(env)

    cost_rows = Generation.read_group(gen_scope, ["total_cost:sum"], [])
    total_consumed = float((cost_rows[0]["total_cost"] if cost_rows else 0.0) or 0.0)
    remaining = max(0.0, budget - total_consumed)

    graph_days = filters["graph_days"]
    window_from = fields.Date.context_today(env.user) - timedelta(days=graph_days - 1)
    window_scope = [
        leaf for leaf in gen_scope if not (isinstance(leaf, (list, tuple)) and len(leaf) == 3 and leaf[0] == "create_date")
    ]
    window_scope += [
        ("create_date", ">=", datetime.combine(window_from, time.min)),
    ]
    window_rows = Generation.read_group(window_scope, ["total_cost:sum"], [])
    window_spend = float((window_rows[0]["total_cost"] if window_rows else 0.0) or 0.0)
    daily_burn = window_spend / graph_days if graph_days else 0.0
    runway_days = (remaining / daily_burn) if daily_burn > 0 else 0.0

    pct = _pct1(total_consumed, budget) if budget else 0.0

    return {
        "budget_count": 1 if budget else 0,
        "total_budget": {"amount": round(budget, 2), "percentage": 100.0 if budget else 0.0},
        "total_consumed": {"amount": round(total_consumed, 2), "percentage": pct},
        "total_remaining": {
            "amount": round(remaining, 2),
            "percentage": round(100.0 - pct, 1) if budget else 0.0,
        },
        "daily_burn_rate": round(daily_burn, 2),
        "runway_days": round(runway_days, 1),
    }


def _build_service_costs(env, gen_scope):
    Generation = env["skoll.generation"].sudo()
    rows = Generation.read_group(
        gen_scope, ["model_arn", "total_cost:sum"], ["model_arn"]
    )
    services = []
    total = 0.0
    for row in rows:
        amount = float(row.get("total_cost") or 0.0)
        total += amount
        services.append({"service_name": row.get("model_arn") or "Unknown", "amount": round(amount, 2)})
    for s in services:
        s["percentage"] = _pct1(s["amount"], total)
    services.sort(key=lambda s: s["amount"], reverse=True)
    return {"total_amount": round(total, 2), "services": services}


def _build_aht_overview(env, gen_scope, target_aht_minutes):
    Generation = env["skoll.generation"].sudo()
    rows = Generation.search_read(gen_scope, ["duration_s"])
    count = 0
    total_seconds = 0.0
    for row in rows:
        d = float(row.get("duration_s") or 0.0)
        if d > 0:
            count += 1
            total_seconds += d
    total_minutes = total_seconds / 60.0
    avg_minutes = (total_minutes / count) if count else 0.0
    if target_aht_minutes > 0:
        if avg_minutes <= target_aht_minutes:
            indicator = "ok"
        else:
            indicator = "over"
    else:
        indicator = "unset"
    return {
        "aht_measured_count": count,
        "aht_total_minutes": round(total_minutes, 2),
        "aht_average_minutes": round(avg_minutes, 2),
        "target_aht_minutes": target_aht_minutes,
        "target_indicator": indicator,
    }


def _build_daily_burn_graph(env, gen_scope, filters):
    Generation = env["skoll.generation"].sudo()
    graph_days = filters["graph_days"]
    today = fields.Date.context_today(env.user)
    window_from = today - timedelta(days=graph_days - 1)
    window_scope = [
        leaf for leaf in gen_scope if not (isinstance(leaf, (list, tuple)) and len(leaf) == 3 and leaf[0] == "create_date")
    ]
    window_scope += [
        ("create_date", ">=", datetime.combine(window_from, time.min)),
        ("create_date", "<=", datetime.combine(today, time.max)),
    ]
    rows = Generation.search_read(window_scope, ["create_date", "total_cost"])
    per_day = defaultdict(float)
    for row in rows:
        cd = row.get("create_date")
        if not cd:
            continue
        if isinstance(cd, str):
            try:
                cd = fields.Datetime.from_string(cd)
            except Exception:
                continue
        per_day[cd.date().isoformat()] += float(row.get("total_cost") or 0.0)

    series = []
    total = 0.0
    peak_day = {"date": None, "amount": 0.0}
    for offset in range(graph_days):
        d = window_from + timedelta(days=offset)
        amount = round(per_day.get(d.isoformat(), 0.0), 4)
        total += amount
        if amount > peak_day["amount"]:
            peak_day = {"date": d.isoformat(), "amount": amount}
        series.append({"date": d.isoformat(), "amount": amount})

    avg = (total / graph_days) if graph_days else 0.0
    return {
        "window": {"start": window_from.isoformat(), "end": today.isoformat()},
        "total_amount": round(total, 4),
        "average_per_day": round(avg, 4),
        "peak_day": peak_day,
        "series": series,
    }


def _build_burn_per_batch(env, gen_scope):
    return {"title": "Burn per batch", "batches": []}


def _build_budget_info_payload(env, project_id, include_inactive, filters):
    gen_scope = _gen_scope(env, filters)
    return {
        "filters": filters,
        "kpi": _build_kpi(env, gen_scope, filters),
        "service_costs": _build_service_costs(env, gen_scope),
        "aht_overview": _build_aht_overview(env, gen_scope, filters["target_aht_minutes"]),
        "daily_burn_graph": _build_daily_burn_graph(env, gen_scope, filters),
        "budget_timeline": {},
        "burn_per_batch": _build_burn_per_batch(env, gen_scope),
        "allocation_ledger": {},
    }


class SkollBudgetController(http.Controller):
    @http.route(
        "/api/v1/skoll_ext/budget/info",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def budget_info(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="Forbidden",
                status=403,
                errors=["User has no Skoll role"],
            )
        try:
            filters = _resolve_filters(kwargs)
            payload = _build_budget_info_payload(
                env, filters["project_id"], filters["include_inactive"], filters
            )
        except Exception as exc:
            _logger.exception("Skoll budget_info failed")
            return return_Response(
                message="Internal Server Error",
                status=500,
                errors=[str(exc)],
            )
        return return_Response(message="OK", status=200, data=payload)

    @http.route(
        "/api/v1/skoll_ext/budget/fetch",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def budget_fetch(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="Forbidden",
                status=403,
                errors=["User has no Skoll role"],
            )
        try:
            filters = _resolve_filters(kwargs)
            payload = _build_budget_info_payload(
                env, filters["project_id"], filters["include_inactive"], filters
            )
            payload["fetch_summary"] = {
                "budgets_total": 0,
                "budgets_fetched": 0,
                "errors": [],
            }
        except Exception as exc:
            _logger.exception("Skoll budget_fetch failed")
            return return_Response(
                message="Internal Server Error",
                status=500,
                errors=[str(exc)],
            )
        return return_Response(message="OK", status=200, data=payload)
