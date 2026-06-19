import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .dashboard import (
    BUDGET_MODEL,
    COST_LINE_MODEL,
    NO_MODEL_LABEL,
    _coerce_date,
    _portfolio_currency,
    _read_json_body,
)

_logger = logging.getLogger(__name__)

BATCH_MODEL = "etp.batch.budget"
TOPUP_MODEL = "etp.project.budget.topup"

BASE_ROUTE = "/api/v1/etp_projects/project_budget_dashboard"

ACTIVE_BATCH_STATES = ("approved", "in_progress", "delivered", "closed")
APPROVED_TOPUP_STATE = "approved"

HEALTH_HEALTHY_PCT = 60.0
HEALTH_WARNING_PCT = 80.0
HEALTH_AT_RISK_PCT = 100.0

ACCURACY_GOOD_PCT = 110.0
UNHEALTHY_HEALTHS = ("warning", "at_risk", "critical")


def _round2(value):
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _safe_pct(part, whole):
    if not whole:
        return 0.0
    return _round2((float(part) / float(whole)) * 100.0)


def _health_from_pct(pct, has_envelope):
    if not has_envelope:
        return "unknown"
    if pct < HEALTH_HEALTHY_PCT:
        return "healthy"
    if pct < HEALTH_WARNING_PCT:
        return "warning"
    if pct < HEALTH_AT_RISK_PCT:
        return "at_risk"
    return "critical"


def _resolve_window(jdata):
    start = _coerce_date(jdata.get("start"), "start")
    end = _coerce_date(jdata.get("end"), "end")
    if start and end:
        if start > end:
            raise ValidationError("'start' must be on or before 'end'.")
        return start, end, "date_range"
    if start or end:
        raise ValidationError("Provide both 'start' and 'end' for a date range.")

    month_str = jdata.get("month")
    if month_str:
        if not isinstance(month_str, str):
            raise ValidationError("'month' must be a string in 'YYYY-MM' format.")
        try:
            mdate = datetime.strptime(month_str, "%Y-%m").date()
        except ValueError:
            raise ValidationError("'month' must be a string in 'YYYY-MM' format.")
    else:
        mdate = date.today()
    m_start = mdate.replace(day=1)
    m_end = (m_start + relativedelta(months=1)) - timedelta(days=1)
    return m_start, m_end, "month"


def _previous_window(start, end):
    return start - relativedelta(months=1), end - relativedelta(months=1)


def _spend_by_budget(budget_ids, start, end):
    if not budget_ids:
        return {}
    groups = request.env[COST_LINE_MODEL].sudo()._read_group(
        [
            ("budget_id", "in", list(budget_ids)),
            ("granularity", "=", "day"),
            ("is_model_breakdown", "=", False),
            ("period", ">=", start),
            ("period", "<=", end),
        ],
        groupby=["budget_id"],
        aggregates=["amount_source:sum"],
    )
    return {b.id: float(a or 0.0) for (b, a) in groups}


def _model_spend_by_budget(budget_ids, start, end):
    out = defaultdict(lambda: defaultdict(float))
    if not budget_ids:
        return out
    groups = request.env[COST_LINE_MODEL].sudo()._read_group(
        [
            ("budget_id", "in", list(budget_ids)),
            ("granularity", "=", "day"),
            ("is_model_breakdown", "=", True),
            ("period", ">=", start),
            ("period", "<=", end),
        ],
        groupby=["budget_id", "model_name"],
        aggregates=["amount_source:sum"],
    )
    for (b, mn, amt) in groups:
        key = mn or NO_MODEL_LABEL
        out[b.id][key] += float(amt or 0.0)
    return out


def _model_breakdown_rows(current_models, previous_models, current_total):
    keys = sorted(set(current_models) | set(previous_models))
    rows = []
    for mn in keys:
        cur = float(current_models.get(mn, 0.0))
        prev = float(previous_models.get(mn, 0.0))
        change = ((cur - prev) / prev * 100.0) if prev else (100.0 if cur else 0.0)
        share = (cur / current_total * 100.0) if current_total else 0.0
        rows.append({
            "model_name": mn,
            "current": _round2(cur),
            "last_month": _round2(prev),
            "change_pct_vs_last_month": _round2(change),
            "share_pct_current": _round2(share),
        })
    rows.sort(key=lambda r: r["current"], reverse=True)
    return rows


def _topups_by_budget(budget_ids):
    if not budget_ids:
        return {}
    groups = request.env[TOPUP_MODEL].sudo()._read_group(
        [
            ("project_budget_id", "in", list(budget_ids)),
            ("state", "=", APPROVED_TOPUP_STATE),
        ],
        groupby=["project_budget_id"],
        aggregates=["amount:sum"],
    )
    return {b.id: float(a or 0.0) for (b, a) in groups}


def _batches_by_budget(budget_ids, start, end):
    out = defaultdict(lambda: {"estimated": 0.0, "approved": 0.0})
    if not budget_ids:
        return out
    domain = [
        ("project_budget_id", "in", list(budget_ids)),
        ("state", "in", list(ACTIVE_BATCH_STATES)),
    ]
    if start and end:
        domain += [
            ("start_date", "<=", end),
            ("end_date", ">=", start),
        ]
    batches = request.env[BATCH_MODEL].sudo().search(domain)
    for bx in batches:
        bucket = out[bx.project_budget_id.id]
        bucket["estimated"] += float(bx.estimated_cost or 0.0)
        bucket["approved"] += float(bx.approved_amount or 0.0)
    return out


def _project_row(budget, spend, topups, batches, days, models_cur, models_prev):
    is_rnd = (budget.project_type == "rnd")
    budget_amount = float(budget.budget_amount or 0.0)
    topup_total = float(topups.get(budget.id, 0.0))
    final_budget = budget_amount + topup_total
    actual = float(spend.get(budget.id, 0.0))

    if is_rnd:
        expected = 0.0
        approved_total = final_budget
    else:
        bucket = batches.get(budget.id) or {"estimated": 0.0, "approved": 0.0}
        expected = float(bucket["estimated"])
        approved_total = float(bucket["approved"])

    consumed_pct = (actual / final_budget * 100.0) if final_budget else 0.0
    health = _health_from_pct(consumed_pct, final_budget > 0)
    health_score = (
        _round2(max(0.0, 100.0 - consumed_pct)) if final_budget else None
    )

    if is_rnd or expected <= 0:
        accuracy_score = None
        actual_vs_estimated_pct = None
    else:
        accuracy_score = _round2((actual / expected) * 100.0)
        actual_vs_estimated_pct = accuracy_score

    pct_over_original = (
        _round2(((actual - final_budget) / final_budget) * 100.0)
        if final_budget else 0.0
    )

    over_budget = bool(final_budget and actual > final_budget)

    risk_amount = max(0.0, actual - approved_total)
    amount_at_risk_pct = (
        _round2((risk_amount / final_budget) * 100.0) if final_budget else 0.0
    )

    run_per_day = (actual / days) if days else 0.0
    run_pct_per_day = (
        _round2((run_per_day / final_budget) * 100.0) if final_budget else 0.0
    )

    current_models = models_cur.get(budget.id, {})
    previous_models = models_prev.get(budget.id, {})
    model_rows = _model_breakdown_rows(current_models, previous_models, actual)

    return {
        "project_id": budget.project_id.id if budget.project_id else False,
        "project_name": budget.project_id.display_name if budget.project_id else "",
        "budget_id": budget.id,
        "budget_name": budget.name,
        "budget_type": budget.project_type or False,
        "is_rnd": is_rnd,
        "budget_amount": _round2(budget_amount),
        "topup_total": _round2(topup_total),
        "final_budget": _round2(final_budget),
        "approved_total": _round2(approved_total),
        "expected_cost": _round2(expected) if not is_rnd else None,
        "total_spend_llm": _round2(actual),
        "actual_spend": _round2(actual),
        "remaining": _round2(final_budget - actual),
        "consumed_pct": _round2(consumed_pct),
        "amount_at_risk_pct": amount_at_risk_pct,
        "run_per_day": _round2(run_per_day),
        "run_pct_per_day": run_pct_per_day,
        "health": health,
        "health_score": health_score,
        "accuracy_score": accuracy_score,
        "actual_vs_estimated_pct": actual_vs_estimated_pct,
        "pct_over_original_budget": pct_over_original,
        "over_budget": over_budget,
        "model_consumption": model_rows,
    }


def _actual_vs_estimated_chart(rows):
    chart = []
    for r in rows:
        if r["is_rnd"]:
            continue
        estimated = r["expected_cost"] or 0.0
        actual = r["actual_spend"]
        chart.append({
            "project_id": r["project_id"],
            "project_name": r["project_name"],
            "budget_id": r["budget_id"],
            "budget_name": r["budget_name"],
            "actual": actual,
            "estimated": _round2(estimated),
            "delta": _round2(actual - estimated),
            "actual_vs_estimated_pct": (
                _round2((actual / estimated) * 100.0) if estimated > 0 else 0.0
            ),
        })
    chart.sort(key=lambda r: r["actual"], reverse=True)
    return chart


def _spend_by_model_chart(models_cur):
    by_model = defaultdict(float)
    total = 0.0
    for bid_models in models_cur.values():
        for mn, amt in bid_models.items():
            by_model[mn] += float(amt or 0.0)
            total += float(amt or 0.0)
    rows = []
    for mn in sorted(by_model, key=lambda k: by_model[k], reverse=True):
        amt = by_model[mn]
        rows.append({
            "model_name": mn,
            "spend": _round2(amt),
            "share_pct": _safe_pct(amt, total),
        })
    return {"rows": rows, "total_spend": _round2(total)}


def _phase_budget_chart(budget_ids, start=None, end=None):
    if not budget_ids:
        return []
    domain = [
        ("project_budget_id", "in", list(budget_ids)),
        ("state", "in", list(ACTIVE_BATCH_STATES)),
    ]
    if start and end:
        domain += [
            ("start_date", "<=", end),
            ("end_date", ">=", start),
        ]
    batches = request.env[BATCH_MODEL].sudo().search(domain)
    rows = []
    for bx in batches:
        budget_amt = float(bx.approved_amount or 0.0)
        estimated = float(bx.estimated_cost or 0.0)
        actual = float(bx.consumed_cost or 0.0)
        rows.append({
            "batch_id": bx.id,
            "phase_name": bx.name,
            "batch_name": bx.name,
            "project_id": bx.project_id.id if bx.project_id else False,
            "project_name": bx.project_id.display_name if bx.project_id else "",
            "project_budget_id": bx.project_budget_id.id,
            "state": bx.state,
            "start_date": bx.start_date.isoformat() if bx.start_date else None,
            "end_date": bx.end_date.isoformat() if bx.end_date else None,
            "budget": _round2(budget_amt),
            "estimated": _round2(estimated),
            "actual": _round2(actual),
            "delta_actual_vs_estimated": _round2(actual - estimated),
            "delta_actual_vs_budget": _round2(actual - budget_amt),
            "estimated_vs_budget_pct": (
                _round2((estimated / budget_amt) * 100.0) if budget_amt else 0.0
            ),
            "actual_vs_budget_pct": (
                _round2((actual / budget_amt) * 100.0) if budget_amt else 0.0
            ),
            "actual_vs_estimated_pct": (
                _round2((actual / estimated) * 100.0) if estimated else 0.0
            ),
        })
    rows.sort(key=lambda r: r["actual"], reverse=True)
    return rows


class EtpProjectBudgetDashboardController(http.Controller):

    @http.route(
        BASE_ROUTE + "/kpis",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def kpis(self, **params):
        try:
            jdata = _read_json_body()
            start, end, filter_type = _resolve_window(jdata)
            days = max(1, (end - start).days + 1)
            prev_start, prev_end = _previous_window(start, end)

            budgets = request.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
            bids = budgets.ids

            spend = _spend_by_budget(bids, start, end)
            topups = _topups_by_budget(bids)
            batches = _batches_by_budget(bids, start, end)
            models_cur = _model_spend_by_budget(bids, start, end)
            models_prev = _model_spend_by_budget(bids, prev_start, prev_end)

            rows = [
                _project_row(b, spend, topups, batches, days, models_cur, models_prev)
                for b in budgets
            ]
            rows.sort(key=lambda r: r["consumed_pct"], reverse=True)

            project_ids = {r["project_id"] for r in rows if r["project_id"]}
            project_count = len(project_ids)

            total_spend_llm = sum(r["actual_spend"] for r in rows)
            total_budget = sum(r["budget_amount"] for r in rows)
            total_topups = sum(r["topup_total"] for r in rows)
            total_final_budget = total_budget + total_topups

            ops_rows = [r for r in rows if not r["is_rnd"]]
            expected_total = sum((r["expected_cost"] or 0.0) for r in ops_rows)
            approved_total = sum(r["approved_total"] for r in ops_rows)
            ops_actual = sum(r["actual_spend"] for r in ops_rows)

            actual_vs_estimated_pct = (
                _round2((ops_actual / expected_total) * 100.0)
                if expected_total > 0 else 0.0
            )
            pct_over_original_budget = (
                _round2(((total_spend_llm - total_final_budget) / total_final_budget) * 100.0)
                if total_final_budget else 0.0
            )

            over_budget_rows = [r for r in rows if r["over_budget"]]
            over_budget_project_count = len({
                r["project_id"] for r in over_budget_rows if r["project_id"]
            })

            accurate_rows = [
                r for r in ops_rows
                if r["accuracy_score"] is not None
                and r["accuracy_score"] <= ACCURACY_GOOD_PCT
            ]
            estimation_accuracy_count = len(accurate_rows)
            estimation_accuracy_score = (
                _round2((ops_actual / expected_total) * 100.0)
                if expected_total > 0 else None
            )

            health_counts = defaultdict(int)
            for r in rows:
                health_counts[r["health"]] += 1

            unhealthy_count = sum(
                health_counts.get(h, 0) for h in UNHEALTHY_HEALTHS
            )
            combined_consumed_pct = (
                (total_spend_llm / total_final_budget * 100.0)
                if total_final_budget else 0.0
            )
            combined_health = _health_from_pct(
                combined_consumed_pct, total_final_budget > 0
            )
            run_per_day_total = total_spend_llm / days if days else 0.0
            run_pct_per_day_total = (
                _round2((run_per_day_total / total_final_budget) * 100.0)
                if total_final_budget else 0.0
            )
            estimation_pct = (
                _round2((ops_actual / expected_total) * 100.0)
                if expected_total > 0 else 0.0
            )
            budget_amount_pct = (
                _round2((total_spend_llm / total_budget) * 100.0)
                if total_budget else 0.0
            )

            actual_vs_estimated_chart = _actual_vs_estimated_chart(rows)
            spend_by_model = _spend_by_model_chart(models_cur)
            phase_budget_chart = _phase_budget_chart(bids, start, end)

            payload = {
                **_portfolio_currency(budgets),
                "filters": {
                    "filter_type": filter_type,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "days": days,
                    "previous_start": prev_start.isoformat(),
                    "previous_end": prev_end.isoformat(),
                },
                "kpis": {
                    "project_count": project_count,
                    "budget_count": len(rows),
                    "total_spend_llm": _round2(total_spend_llm),
                    "actual_spend": _round2(total_spend_llm),
                    "expected_total": _round2(expected_total),
                    "estimated_cost": _round2(expected_total),
                    "approved_total": _round2(approved_total),
                    "total_approved_budget": _round2(approved_total),
                    "total_budget": _round2(total_budget),
                    "total_topups": _round2(total_topups),
                    "project_final_budget": _round2(total_final_budget),
                    "actual_vs_estimated_pct": actual_vs_estimated_pct,
                    "pct_over_original_budget": pct_over_original_budget,
                    "over_budget_project_count": over_budget_project_count,
                    "estimation_accuracy_count": estimation_accuracy_count,
                    "estimation_accuracy_score": estimation_accuracy_score,
                    "health_breakdown": {
                        "healthy": health_counts.get("healthy", 0),
                        "warning": health_counts.get("warning", 0),
                        "at_risk": health_counts.get("at_risk", 0),
                        "critical": health_counts.get("critical", 0),
                        "unknown": health_counts.get("unknown", 0),
                    },
                },
                "project_over_estimation": {
                    "unhealthy_project_count": unhealthy_count,
                    "combined_health": combined_health,
                    "combined_consumed_pct": _round2(combined_consumed_pct),
                    "project_estimated_amount": _round2(expected_total),
                    "actual_spend_amount": _round2(total_spend_llm),
                    "estimation_pct": estimation_pct,
                    "budget_amount_pct": budget_amount_pct,
                    "run_per_day": _round2(run_per_day_total),
                    "run_pct_per_day": run_pct_per_day_total,
                },
                "projects": {
                    "row_count": len(rows),
                    "rows": rows,
                },
                "actual_vs_estimated_chart": {
                    "row_count": len(actual_vs_estimated_chart),
                    "rows": actual_vs_estimated_chart,
                },
                "spend_by_model": spend_by_model,
                "phase_budget_chart": {
                    "row_count": len(phase_budget_chart),
                    "rows": phase_budget_chart,
                },
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("project_budget_dashboard.kpis failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        BASE_ROUTE + "/actual_vs_estimated",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def actual_vs_estimated(self, **params):
        try:
            jdata = _read_json_body()
            start, end, filter_type = _resolve_window(jdata)
            budgets = request.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
            bids = budgets.ids
            spend = _spend_by_budget(bids, start, end)
            batches = _batches_by_budget(bids, start, end)

            rows = []
            for b in budgets:
                if b.project_type == "rnd":
                    continue
                bucket = batches.get(b.id) or {"estimated": 0.0, "approved": 0.0}
                estimated = float(bucket["estimated"])
                actual = float(spend.get(b.id, 0.0))
                rows.append({
                    "project_id": b.project_id.id if b.project_id else False,
                    "project_name": b.project_id.display_name if b.project_id else "",
                    "budget_id": b.id,
                    "budget_name": b.name,
                    "actual": _round2(actual),
                    "estimated": _round2(estimated),
                    "delta": _round2(actual - estimated),
                    "actual_vs_estimated_pct": (
                        _round2((actual / estimated) * 100.0) if estimated > 0 else 0.0
                    ),
                })
            rows.sort(key=lambda r: r["actual"], reverse=True)

            total_actual = sum(r["actual"] for r in rows)
            total_estimated = sum(r["estimated"] for r in rows)

            payload = {
                **_portfolio_currency(budgets),
                "filters": {
                    "filter_type": filter_type,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                "row_count": len(rows),
                "rows": rows,
                "totals": {
                    "actual": _round2(total_actual),
                    "estimated": _round2(total_estimated),
                    "delta": _round2(total_actual - total_estimated),
                    "actual_vs_estimated_pct": (
                        _round2((total_actual / total_estimated) * 100.0)
                        if total_estimated > 0 else 0.0
                    ),
                },
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("project_budget_dashboard.actual_vs_estimated failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        BASE_ROUTE + "/spend_by_model",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def spend_by_model(self, **params):
        try:
            jdata = _read_json_body()
            start, end, filter_type = _resolve_window(jdata)
            budgets = request.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
            bids = budgets.ids
            models_cur = _model_spend_by_budget(bids, start, end)
            chart = _spend_by_model_chart(models_cur)

            payload = {
                **_portfolio_currency(budgets),
                "filters": {
                    "filter_type": filter_type,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                "row_count": len(chart["rows"]),
                "rows": chart["rows"],
                "total_spend": chart["total_spend"],
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("project_budget_dashboard.spend_by_model failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        BASE_ROUTE + "/phase_budget",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def phase_budget(self, **params):
        try:
            jdata = _read_json_body()
            start, end, filter_type = _resolve_window(jdata)
            budgets = request.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
            bids = budgets.ids
            rows = _phase_budget_chart(bids, start, end)

            total_budget = sum(r["budget"] for r in rows)
            total_estimated = sum(r["estimated"] for r in rows)
            total_actual = sum(r["actual"] for r in rows)

            payload = {
                **_portfolio_currency(budgets),
                "filters": {
                    "filter_type": filter_type,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                "row_count": len(rows),
                "rows": rows,
                "totals": {
                    "budget": _round2(total_budget),
                    "estimated": _round2(total_estimated),
                    "actual": _round2(total_actual),
                    "delta_actual_vs_estimated": _round2(total_actual - total_estimated),
                    "delta_actual_vs_budget": _round2(total_actual - total_budget),
                    "actual_vs_budget_pct": (
                        _round2((total_actual / total_budget) * 100.0)
                        if total_budget else 0.0
                    ),
                    "actual_vs_estimated_pct": (
                        _round2((total_actual / total_estimated) * 100.0)
                        if total_estimated else 0.0
                    ),
                },
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("project_budget_dashboard.phase_budget failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )
