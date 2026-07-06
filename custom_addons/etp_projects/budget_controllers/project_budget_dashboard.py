import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from odoo import fields, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from ..controllers.dashboard import (
    BUDGET_MODEL,
    COST_LINE_MODEL,
    _coerce_date,
    _coerce_id_list,
    _portfolio_currency,
    _read_json_body,
)

_logger = logging.getLogger(__name__)

BATCH_MODEL = "etp.batch.budget"
DAILY_TASK_MODEL = "etp.batch.budget.daily.task"
DAILY_TASK_MODEL_MODEL = "etp.batch.budget.daily.task.model"
BATCH_INFRA_LINE_MODEL = "etp.batch.budget.infra.line"
BATCH_SUBSCRIPTION_LINE_MODEL = "etp.batch.budget.subscription.line"
BATCH_MODEL_LINE_MODEL = "etp.batch.budget.model.line"
AI_MODEL_MODEL = "etp.ai.model"
SUBSCRIPTION_MODEL = "etp.subscription"
PROJECT_MODEL = "project.project"

BASE_ROUTE = "/api/v1/etp_projects/budget/project_budget_dashboard"

HEALTH_HEALTHY_PCT = 60.0
HEALTH_WARNING_PCT = 80.0
HEALTH_AT_RISK_PCT = 100.0
ACCURACY_GOOD_PCT = 110.0

ACTIVE_BATCH_STATES = ("approved", "in_progress", "delivered", "closed")
UNHEALTHY_HEALTHS = ("warning", "at_risk", "critical")
PRODUCTION_PROJECT_STATE = "production"
RISK_WINDOW_DAYS = 7


def _health_bucket(consumed_pct):
    if consumed_pct < HEALTH_HEALTHY_PCT:
        return "healthy"
    if consumed_pct < HEALTH_WARNING_PCT:
        return "warning"
    if consumed_pct < HEALTH_AT_RISK_PCT:
        return "at_risk"
    return "critical"


def _health_score(consumed_pct):
    return round(max(0.0, 100.0 - float(consumed_pct or 0.0)), 2)


def _pct_diff(a, b):
    if not b:
        return 0.0
    return round((float(a) - float(b)) / float(b) * 100.0, 2)


def _accuracy_pct(numerator, denominator):
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator) * 100.0, 2)


def _round2(value):
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _pct(part, whole):
    if not whole:
        return 0.0
    return _round2((float(part or 0.0) / float(whole)) * 100.0)


def _date_str(d):
    return d.strftime("%Y-%m-%d") if d else ""


def _in_window(day, start, end):
    if not day:
        return False
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


def _phase_actual_from_cost_lines(batch, start, end):
    lines = batch.matched_cost_line_ids
    if start or end:
        lines = lines.filtered(lambda cl: _in_window(cl.period, start, end))
    return sum(float(cl.amount_source or 0.0) for cl in lines)


def _phase_expected_from_daily_tasks(batch, start, end):
    tasks = batch.daily_task_ids
    if start or end:
        tasks = tasks.filtered(lambda t: _in_window(t.entry_date, start, end))
    return sum(float(t.total_cost or 0.0) for t in tasks)


def _phase_budget_amount(batch):
    approved = float(batch.approved_amount or 0.0)
    if approved:
        return approved
    return float(batch.batch_budget or 0.0)


def _filter_budgets(jdata):
    project_ids = _coerce_id_list(jdata.get("project_ids"), "project_ids")
    include_inactive = bool(jdata.get("include_inactive"))
    Budget = request.env[BUDGET_MODEL].sudo()
    domain = []
    if not include_inactive:
        domain.append(("active", "=", True))
    if project_ids:
        domain.append(("project_id", "in", project_ids))
    return Budget.search(domain), project_ids


def _parse_range(jdata):
    start = _coerce_date(jdata.get("start"), "start")
    end = _coerce_date(jdata.get("end"), "end")
    if start and end and start > end:
        raise ValidationError("'start' must be on or before 'end'.")
    return start, end


def _phase_infra_actual(batch, start, end):
    tasks = batch.daily_task_ids
    if start or end:
        tasks = tasks.filtered(lambda t: _in_window(t.entry_date, start, end))
    return sum(float(t.infra_cost or 0.0) for t in tasks)


def _phase_subscription_actual(batch, start, end):
    tasks = batch.daily_task_ids
    if start or end:
        tasks = tasks.filtered(lambda t: _in_window(t.entry_date, start, end))
    return sum(float(t.subscription_cost or 0.0) for t in tasks)


def _user_role(user):
    if not user:
        return ""
    partner = user.partner_id
    if partner and partner.function:
        return partner.function
    groups = user.groups_id.filtered(lambda g: g.category_id and g.category_id.name)
    if groups:
        return groups[0].category_id.name + " / " + groups[0].name
    return ""


def _model_estimation_from_daily_tasks(batch, start, end):
    """Return {ai_model_id: estimation_amount} for a batch within window.

    Prefers per-day model breakdown when present; otherwise falls back to
    splitting daily_task.total_cost proportionally by model_line.per_task_cost.
    """
    result = defaultdict(float)
    model_line_weights = {}
    for ml in batch.model_line_ids:
        if ml.ai_model_id:
            model_line_weights[ml.ai_model_id.id] = float(ml.per_task_cost or 0.0)
    total_weight = sum(model_line_weights.values())

    for task in batch.daily_task_ids:
        if not _in_window(task.entry_date, start, end):
            continue
        breakdown = getattr(task, "model_breakdown_ids", False)
        if breakdown:
            for br in breakdown:
                if br.ai_model_id:
                    result[br.ai_model_id.id] += float(br.ideal_cost or 0.0)
            continue
        total_cost = float(task.total_cost or 0.0)
        if not total_cost or total_weight <= 0.0:
            continue
        for model_id, weight in model_line_weights.items():
            result[model_id] += total_cost * (weight / total_weight)
    return result


def _model_actual_by_name(project_ids, model_names, start, end):
    if not project_ids or not model_names:
        return {}
    domain = [
        ("project_id", "in", list(project_ids)),
        ("granularity", "=", "day"),
        ("model_name", "in", list(model_names)),
    ]
    if start:
        domain.append(("period", ">=", start))
    if end:
        domain.append(("period", "<=", end))
    CostLine = request.env[COST_LINE_MODEL].sudo()
    rows = CostLine.read_group(
        domain,
        fields=["model_name", "amount_source:sum"],
        groupby=["model_name"],
    )
    return {r["model_name"]: float(r["amount_source"] or 0.0) for r in rows if r.get("model_name")}


class EtpProjectBudgetDashboard(http.Controller):

    @http.route(
        BASE_ROUTE + "/kpis",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def kpis(self, **params):
        try:
            jdata = _read_json_body()
            start, end = _parse_range(jdata)
            budgets, project_ids = _filter_budgets(jdata)

            phase_rows = []
            project_agg = defaultdict(lambda: {
                "project_id": False,
                "project_name": "",
                "actual": 0.0,
                "expected": 0.0,
                "budget": 0.0,
                "phase_count": 0,
                "latest_flag_date": None,
                "latest_batch_state": "",
            })

            portfolio_actual = 0.0
            portfolio_expected = 0.0
            portfolio_budget = 0.0

            for budget in budgets:
                pid = budget.project_id.id if budget.project_id else 0
                pname = budget.project_id.name if budget.project_id else ""
                for batch in budget.batch_budget_ids:
                    actual = _phase_actual_from_cost_lines(batch, start, end)
                    expected = _phase_expected_from_daily_tasks(batch, start, end)
                    budget_amt = _phase_budget_amount(batch)
                    consumed_pct = (actual / budget_amt * 100.0) if budget_amt else 0.0

                    phase_rows.append({
                        "batch_id": batch.id,
                        "batch_name": batch.name or "",
                        "batch_state": batch.state or "",
                        "project_id": pid,
                        "project_name": pname,
                        "actual_cost": round(actual, 2),
                        "expected_cost": round(expected, 2),
                        "budget_amount": round(budget_amt, 2),
                        "consumed_pct": round(consumed_pct, 2),
                        "health_score": _health_score(consumed_pct),
                        "health_bucket": _health_bucket(consumed_pct),
                        "accuracy_actual_vs_expected_pct": _accuracy_pct(actual, expected),
                        "accuracy_actual_vs_budget_pct": _accuracy_pct(actual, budget_amt),
                        "start_date": batch.start_date.strftime("%Y-%m-%d") if batch.start_date else "",
                        "end_date": batch.end_date.strftime("%Y-%m-%d") if batch.end_date else "",
                    })

                    portfolio_actual += actual
                    portfolio_expected += expected
                    portfolio_budget += budget_amt

                    agg = project_agg[pid]
                    agg["project_id"] = pid
                    agg["project_name"] = pname
                    agg["actual"] += actual
                    agg["expected"] += expected
                    agg["budget"] += budget_amt
                    agg["phase_count"] += 1
                    flag_day = batch.end_date or (batch.write_date.date() if batch.write_date else None)
                    if flag_day and (not agg["latest_flag_date"] or flag_day > agg["latest_flag_date"]):
                        agg["latest_flag_date"] = flag_day
                        agg["latest_batch_state"] = batch.state or ""

            projects_summary = []
            flagged_projects = []
            over_budget_projects = 0
            health_breakdown = {"healthy": 0, "warning": 0, "at_risk": 0, "critical": 0}
            healthy_project_scores = []

            for pid, agg in project_agg.items():
                actual = agg["actual"]
                expected = agg["expected"]
                budget_amt = agg["budget"]
                consumed_pct = (actual / budget_amt * 100.0) if budget_amt else 0.0
                bucket = _health_bucket(consumed_pct)
                score = _health_score(consumed_pct)
                accuracy_ae = _accuracy_pct(actual, expected)
                accuracy_ab = _accuracy_pct(actual, budget_amt)
                over_budget = actual > budget_amt and budget_amt > 0

                if over_budget:
                    over_budget_projects += 1
                health_breakdown[bucket] += 1
                healthy_project_scores.append(score)

                summary_row = {
                    "project_id": pid,
                    "project_name": agg["project_name"],
                    "phase_count": agg["phase_count"],
                    "actual_cost": round(actual, 2),
                    "expected_cost": round(expected, 2),
                    "budget_amount": round(budget_amt, 2),
                    "consumed_pct": round(consumed_pct, 2),
                    "health_score": score,
                    "health_bucket": bucket,
                    "accuracy_actual_vs_expected_pct": accuracy_ae,
                    "accuracy_actual_vs_budget_pct": accuracy_ab,
                    "over_budget": over_budget,
                }
                projects_summary.append(summary_row)

                is_flagged = (
                    over_budget
                    or bucket in ("at_risk", "critical")
                    or (expected and accuracy_ae > ACCURACY_GOOD_PCT)
                )
                if is_flagged:
                    flag_date = agg["latest_flag_date"]
                    flagged_projects.append({
                        "date": flag_date.strftime("%Y-%m-%d") if flag_date else "",
                        "project_id": pid,
                        "project_name": agg["project_name"],
                        "estimation": round(expected, 2),
                        "actual": round(actual, 2),
                        "budget": round(budget_amt, 2),
                        "health_score": score,
                        "health_bucket": bucket,
                        "accuracy_pct": accuracy_ae,
                        "batch_state": agg["latest_batch_state"],
                    })

            projects_summary.sort(key=lambda r: -r["consumed_pct"])
            flagged_projects.sort(key=lambda r: (r["date"] or ""), reverse=True)

            Project = request.env[PROJECT_MODEL].sudo()
            project_domain = [("non_stemp_project_status", "=", PRODUCTION_PROJECT_STATE)]
            if project_ids:
                project_domain.append(("id", "in", project_ids))
            production_project_count = Project.search_count(project_domain)

            active_project_count = len({
                b.project_id.id for b in budgets
                if b.project_id and any(
                    bt.state in ACTIVE_BATCH_STATES for bt in b.batch_budget_ids
                )
            })

            today = date.today()
            risk_window_start = today - timedelta(days=RISK_WINDOW_DAYS)
            new_in_risk_count = 0
            for b in budgets:
                created = b.create_date.date() if b.create_date else None
                if not created or created < risk_window_start:
                    continue
                pid = b.project_id.id if b.project_id else 0
                summary = next((r for r in projects_summary if r["project_id"] == pid), None)
                if summary and summary["health_bucket"] in UNHEALTHY_HEALTHS:
                    new_in_risk_count += 1

            overall_consumed_pct = (
                portfolio_actual / portfolio_budget * 100.0 if portfolio_budget else 0.0
            )
            overall_health_score = (
                round(sum(healthy_project_scores) / len(healthy_project_scores), 2)
                if healthy_project_scores else 0.0
            )

            estimation_accuracy_pct = _accuracy_pct(portfolio_actual, portfolio_expected)
            actual_vs_budget_pct = _accuracy_pct(portfolio_actual, portfolio_budget)
            expected_vs_actual_diff_pct = _pct_diff(portfolio_expected, portfolio_actual)
            actual_vs_expected_diff_pct = _pct_diff(portfolio_actual, portfolio_expected)

            currency = _portfolio_currency(budgets)

            payload = {
                "window": {
                    "start": start.strftime("%Y-%m-%d") if start else "",
                    "end": end.strftime("%Y-%m-%d") if end else "",
                },
                "filters": {
                    "project_ids": project_ids,
                    "include_inactive": bool(jdata.get("include_inactive")),
                },
                "currency": currency["currency"],
                "currency_symbol": currency["currency_symbol"],
                "totals": {
                    "actual_cost": round(portfolio_actual, 2),
                    "expected_cost": round(portfolio_expected, 2),
                    "budget_amount": round(portfolio_budget, 2),
                    "consumed_pct": round(overall_consumed_pct, 2),
                    "overall_health_score": overall_health_score,
                    "accuracy_actual_vs_expected_pct": estimation_accuracy_pct,
                    "accuracy_actual_vs_budget_pct": actual_vs_budget_pct,
                },
                "portfolio_budget": {
                    "project_count": len(project_agg),
                    "total_budget": round(portfolio_budget, 2),
                },
                "estimated_cost": {
                    "estimated_cost": round(portfolio_expected, 2),
                    "diff_vs_actual_pct": expected_vs_actual_diff_pct,
                },
                "actual_spend": {
                    "actual_cost": round(portfolio_actual, 2),
                    "diff_vs_estimated_pct": actual_vs_expected_diff_pct,
                },
                "counts": {
                    "production_project_count": production_project_count,
                    "active_project_count": active_project_count,
                    "flagged_project_count": len(flagged_projects),
                    "over_budget_project_count": over_budget_projects,
                    "critical_project_count": health_breakdown["critical"],
                    "high_risk_project_count": health_breakdown["at_risk"],
                    "warning_project_count": health_breakdown["warning"],
                    "healthy_project_count": health_breakdown["healthy"],
                    "new_in_risk_this_week_count": new_in_risk_count,
                },
                "health_breakdown": health_breakdown,
                "estimation_accuracy_pct": estimation_accuracy_pct,
                "phase_breakdown": phase_rows,
                "projects": projects_summary,
                "flagged_projects": flagged_projects,
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("budget.project_budget_dashboard.kpis failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        BASE_ROUTE + "/actual_budget_estimation",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def actual_budget_estimation(self, **params):
        try:
            jdata = _read_json_body()
            start, end = _parse_range(jdata)
            budgets, project_ids = _filter_budgets(jdata)

            per_project = {}
            for budget in budgets:
                pid = budget.project_id.id if budget.project_id else 0
                pname = budget.project_id.name if budget.project_id else ""
                row = per_project.setdefault(pid, {
                    "project_id": pid,
                    "project_name": pname,
                    "budget": 0.0,
                    "estimation": 0.0,
                    "actual": 0.0,
                })
                for batch in budget.batch_budget_ids:
                    row["budget"] += _phase_budget_amount(batch)
                    row["estimation"] += _phase_expected_from_daily_tasks(batch, start, end)
                    row["actual"] += _phase_actual_from_cost_lines(batch, start, end)

            rows = []
            totals = {"budget": 0.0, "estimation": 0.0, "actual": 0.0}
            for r in per_project.values():
                budget_amt = r["budget"]
                actual = r["actual"]
                spend_pct = (actual / budget_amt * 100.0) if budget_amt else 0.0
                rows.append({
                    "project_id": r["project_id"],
                    "project_name": r["project_name"],
                    "budget": round(budget_amt, 2),
                    "estimation": round(r["estimation"], 2),
                    "actual": round(actual, 2),
                    "actual_spend_pct_of_budget": round(spend_pct, 2),
                })
                totals["budget"] += budget_amt
                totals["estimation"] += r["estimation"]
                totals["actual"] += actual

            rows.sort(key=lambda x: -x["actual_spend_pct_of_budget"])
            currency = _portfolio_currency(budgets)
            payload = {
                "window": {
                    "start": start.strftime("%Y-%m-%d") if start else "",
                    "end": end.strftime("%Y-%m-%d") if end else "",
                },
                "filters": {"project_ids": project_ids},
                "currency": currency["currency"],
                "currency_symbol": currency["currency_symbol"],
                "totals": {
                    "budget": round(totals["budget"], 2),
                    "estimation": round(totals["estimation"], 2),
                    "actual": round(totals["actual"], 2),
                    "actual_spend_pct_of_budget": round(
                        (totals["actual"] / totals["budget"] * 100.0) if totals["budget"] else 0.0, 2,
                    ),
                },
                "projects": rows,
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("budget.project_budget_dashboard.actual_budget_estimation failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        BASE_ROUTE + "/model_wise_expenses",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def model_wise_expenses(self, **params):
        try:
            jdata = _read_json_body()
            start, end = _parse_range(jdata)
            budgets, project_ids = _filter_budgets(jdata)

            model_budget = defaultdict(float)
            model_estimation = defaultdict(float)
            model_info = {}
            project_id_set = set()

            for budget in budgets:
                if budget.project_id:
                    project_id_set.add(budget.project_id.id)
                for batch in budget.batch_budget_ids:
                    for ml in batch.model_line_ids:
                        if not ml.ai_model_id:
                            continue
                        model_id = ml.ai_model_id.id
                        approved = float(ml.approved_amount or 0.0)
                        if not approved:
                            approved = float(ml.per_task_cost or 0.0) * float(batch.total_tasks or 0)
                        model_budget[model_id] += approved
                        model_info.setdefault(model_id, {
                            "id": model_id,
                            "name": ml.ai_model_id.name or "",
                        })
                    for model_id, est in _model_estimation_from_daily_tasks(batch, start, end).items():
                        model_estimation[model_id] += est
                        if model_id not in model_info:
                            ai = request.env[AI_MODEL_MODEL].sudo().browse(model_id)
                            if ai.exists():
                                model_info[model_id] = {"id": model_id, "name": ai.name or ""}

            model_names = [info["name"] for info in model_info.values() if info["name"]]
            actual_by_name = _model_actual_by_name(project_id_set, model_names, start, end)

            rows = []
            totals = {"budget": 0.0, "estimation": 0.0, "actual": 0.0}
            for model_id, info in model_info.items():
                name = info["name"]
                budget_amt = model_budget.get(model_id, 0.0)
                estimation = model_estimation.get(model_id, 0.0)
                actual = float(actual_by_name.get(name, 0.0))
                consumed_pct = (actual / budget_amt * 100.0) if budget_amt else 0.0
                remaining_pct = max(0.0, 100.0 - consumed_pct)
                rows.append({
                    "model_id": model_id,
                    "model_name": name,
                    "budget": round(budget_amt, 2),
                    "estimation": round(estimation, 2),
                    "actual": round(actual, 2),
                    "consumed_pct_of_budget": round(consumed_pct, 2),
                    "remaining_pct_of_budget": round(remaining_pct, 2),
                })
                totals["budget"] += budget_amt
                totals["estimation"] += estimation
                totals["actual"] += actual

            rows.sort(key=lambda x: -x["actual"])
            currency = _portfolio_currency(budgets)
            payload = {
                "window": {
                    "start": start.strftime("%Y-%m-%d") if start else "",
                    "end": end.strftime("%Y-%m-%d") if end else "",
                },
                "filters": {"project_ids": project_ids},
                "currency": currency["currency"],
                "currency_symbol": currency["currency_symbol"],
                "totals": {
                    "budget": round(totals["budget"], 2),
                    "estimation": round(totals["estimation"], 2),
                    "actual": round(totals["actual"], 2),
                    "consumed_pct_of_budget": round(
                        (totals["actual"] / totals["budget"] * 100.0) if totals["budget"] else 0.0, 2,
                    ),
                },
                "models": rows,
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("budget.project_budget_dashboard.model_wise_expenses failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        BASE_ROUTE + "/infrastructure_by_projects",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def infrastructure_by_projects(self, **params):
        try:
            jdata = _read_json_body()
            start, end = _parse_range(jdata)
            budgets, project_ids = _filter_budgets(jdata)

            per_project = {}
            user_rows = []
            seen_user_project = set()

            for budget in budgets:
                pid = budget.project_id.id if budget.project_id else 0
                pname = budget.project_id.name if budget.project_id else ""
                row = per_project.setdefault(pid, {
                    "project_id": pid,
                    "project_name": pname,
                    "infra_cost": 0.0,
                    "subscription_count": 0,
                    "subscriptions": set(),
                    "user_count": 0,
                    "user_ids": set(),
                })
                for batch in budget.batch_budget_ids:
                    row["infra_cost"] += _phase_infra_actual(batch, start, end)
                    for sub_line in batch.subscription_line_ids:
                        if sub_line.subscription_id:
                            row["subscriptions"].add(sub_line.subscription_id.id)
                        for user in sub_line.assigned_user_ids:
                            row["user_ids"].add(user.id)
                            key = (user.id, pid)
                            if key in seen_user_project:
                                continue
                            seen_user_project.add(key)
                            user_rows.append({
                                "user_id": user.id,
                                "user_name": user.name or "",
                                "role": _user_role(user),
                                "project_id": pid,
                                "project_name": pname,
                                "subscription_name": (
                                    sub_line.subscription_id.name if sub_line.subscription_id else ""
                                ),
                            })

            rows = []
            totals = {"infra_cost": 0.0, "subscription_count": 0, "user_count": 0}
            for r in per_project.values():
                sub_count = len(r["subscriptions"])
                user_count = len(r["user_ids"])
                rows.append({
                    "project_id": r["project_id"],
                    "project_name": r["project_name"],
                    "infra_cost": round(r["infra_cost"], 2),
                    "subscription_count": sub_count,
                    "user_count": user_count,
                })
                totals["infra_cost"] += r["infra_cost"]
                totals["subscription_count"] += sub_count
                totals["user_count"] += user_count

            rows.sort(key=lambda x: -x["infra_cost"])
            user_rows.sort(key=lambda x: (x["project_name"], x["user_name"]))
            currency = _portfolio_currency(budgets)
            payload = {
                "window": {
                    "start": start.strftime("%Y-%m-%d") if start else "",
                    "end": end.strftime("%Y-%m-%d") if end else "",
                },
                "filters": {"project_ids": project_ids},
                "currency": currency["currency"],
                "currency_symbol": currency["currency_symbol"],
                "totals": {
                    "infra_cost": round(totals["infra_cost"], 2),
                    "subscription_count": totals["subscription_count"],
                    "user_count": totals["user_count"],
                },
                "projects": rows,
                "user_assignments": user_rows,
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("budget.project_budget_dashboard.infrastructure_by_projects failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        BASE_ROUTE + "/subscription_table",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def subscription_table(self, **params):
        try:
            jdata = _read_json_body()
            budgets, project_ids = _filter_budgets(jdata)

            batch_ids = []
            project_by_batch = {}
            for budget in budgets:
                for batch in budget.batch_budget_ids:
                    batch_ids.append(batch.id)
                    project_by_batch[batch.id] = (
                        budget.project_id.id if budget.project_id else 0,
                        budget.project_id.name if budget.project_id else "",
                    )

            SubLine = request.env[BATCH_SUBSCRIPTION_LINE_MODEL].sudo()
            lines = SubLine.search([("batch_id", "in", batch_ids)]) if batch_ids else SubLine.browse([])

            agg = {}
            for line in lines:
                sub = line.subscription_id
                if not sub:
                    continue
                bucket = agg.setdefault(sub.id, {
                    "subscription_id": sub.id,
                    "subscription_name": sub.name or "",
                    "monthly_cost": float(sub.cost or 0.0),
                    "approved_amount": 0.0,
                    "user_ids": set(),
                    "project_ids": set(),
                    "project_names": set(),
                    "user_details": {},
                })
                bucket["approved_amount"] += float(line.approved_amount or 0.0)
                pid, pname = project_by_batch.get(line.batch_id.id, (0, ""))
                if pid:
                    bucket["project_ids"].add(pid)
                if pname:
                    bucket["project_names"].add(pname)
                for user in line.assigned_user_ids:
                    bucket["user_ids"].add(user.id)
                    if user.id not in bucket["user_details"]:
                        bucket["user_details"][user.id] = {
                            "user_id": user.id,
                            "user_name": user.name or "",
                            "role": _user_role(user),
                        }

            rows = []
            for bucket in agg.values():
                rows.append({
                    "subscription_id": bucket["subscription_id"],
                    "subscription_name": bucket["subscription_name"],
                    "monthly_cost": round(bucket["monthly_cost"], 2),
                    "approved_amount": round(bucket["approved_amount"], 2),
                    "people_count": len(bucket["user_ids"]),
                    "project_count": len(bucket["project_ids"]),
                    "projects": sorted(bucket["project_names"]),
                    "assigned_users": sorted(
                        bucket["user_details"].values(), key=lambda u: u["user_name"],
                    ),
                })

            rows.sort(key=lambda x: -x["approved_amount"])
            currency = _portfolio_currency(budgets)
            payload = {
                "filters": {"project_ids": project_ids},
                "currency": currency["currency"],
                "currency_symbol": currency["currency_symbol"],
                "totals": {
                    "subscription_count": len(rows),
                    "monthly_cost": round(sum(r["monthly_cost"] for r in rows), 2),
                    "approved_amount": round(sum(r["approved_amount"] for r in rows), 2),
                    "people_count": len({
                        uid for bucket in agg.values() for uid in bucket["user_ids"]
                    }),
                },
                "subscriptions": rows,
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("budget.project_budget_dashboard.subscription_table failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        BASE_ROUTE + "/project_summary",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_summary(self, **params):
        """Project → batch → (model / infra / subscription) budget rollup.

        Non-obvious contract:
          - estimation       = sum(daily_task.total_cost) — the daily done-task cost log.
          - budget           = batch approved_amount (drafts contribute 0).
          - run_rate_per_day = actual / days_elapsed, where
                               days_elapsed = inclusive days from batch.start_date
                               to min(today, batch.end_date), clamped to >= 1.
          - Batches in state 'rejected' or 'withdrawn' are excluded.
        """
        try:
            jdata = _read_json_body()
            start, end = _parse_range(jdata)
            budgets, project_ids = _filter_budgets(jdata)

            today = date.today()

            def _fmt_date(d):
                return d.strftime("%Y-%m-%d") if d else ""

            def _days_elapsed(batch):
                start_d = batch.start_date
                end_d = batch.end_date
                if not start_d:
                    return 1
                effective_end = min(today, end_d) if end_d else today
                if effective_end < start_d:
                    return 1
                return max(1, (effective_end - start_d).days + 1)

            def _metrics(budget_amt, estimation, actual, days_elapsed):
                variance = estimation - actual
                remaining = budget_amt - actual
                util_pct = (actual / budget_amt * 100.0) if budget_amt else 0.0
                run_rate = (actual / days_elapsed) if days_elapsed else 0.0
                return {
                    "budget": round(budget_amt, 2),
                    "estimation": round(estimation, 2),
                    "actual": round(actual, 2),
                    "variance": round(variance, 2),
                    "remaining": round(remaining, 2),
                    "util_pct": round(util_pct, 2),
                    "run_rate_per_day": round(run_rate, 2),
                }

            project_group = defaultdict(list)
            project_meta = {}
            for budget in budgets:
                if not budget.project_id:
                    continue
                pid = budget.project_id.id
                project_meta[pid] = budget.project_id
                project_group[pid].append(budget)

            projects_payload = []
            totals = {"budget": 0.0, "estimation": 0.0, "actual": 0.0, "run_rate_per_day": 0.0}

            for pid, project_budgets in project_group.items():
                proj = project_meta[pid]
                proj_budget = proj_est = proj_actual = proj_run_rate = 0.0
                batches_payload = []

                for budget in project_budgets:
                    for batch in budget.batch_budget_ids:
                        if batch.state in ("rejected", "withdrawn"):
                            continue

                        b_budget = _phase_budget_amount(batch)
                        b_est = _phase_expected_from_daily_tasks(batch, start, end)
                        b_actual = _phase_actual_from_cost_lines(batch, start, end)
                        days = _days_elapsed(batch)
                        b_metrics = _metrics(b_budget, b_est, b_actual, days)

                        # ── Model-wise breakdown ────────────────────────
                        est_by_model = _model_estimation_from_daily_tasks(batch, start, end)
                        models_payload = []
                        for ml in batch.model_line_ids:
                            if not ml.ai_model_id:
                                continue
                            m_budget = float(ml.approved_amount or 0.0)
                            m_est = float(est_by_model.get(ml.ai_model_id.id, 0.0))
                            m_actual = float(ml.consumed_amount or 0.0)
                            m_metrics = _metrics(m_budget, m_est, m_actual, days)
                            models_payload.append({
                                "ai_model_id": ml.ai_model_id.id,
                                "model_name": ml.ai_model_id.name or "",
                                "provider": ml.ai_model_id.provider or "",
                                "cost_type": ml.cost_type or "",
                                "per_task_cost": round(float(ml.per_task_cost or 0.0), 4),
                                **m_metrics,
                            })

                        # ── Infra-wise breakdown ────────────────────────
                        infra_payload = []
                        for il in batch.infra_line_ids:
                            if not il.infra_type_id:
                                continue
                            i_budget = float(il.approved_amount or 0.0)
                            i_est = float(il.budget_amount or 0.0)
                            i_actual = float(il.consumed_amount or 0.0)
                            i_metrics = _metrics(i_budget, i_est, i_actual, days)
                            infra_payload.append({
                                "infra_type_id": il.infra_type_id.id,
                                "infra_name": il.infra_type_id.name or "",
                                "provider": il.infra_type_id.code or "",
                                "description": il.description or "",
                                "per_day_cost": round(float(il.per_day_cost or 0.0), 2),
                                "start_date": _fmt_date(il.start_date),
                                "end_date": _fmt_date(il.end_date),
                                **i_metrics,
                            })

                        # ── Subscription info ───────────────────────────
                        subs_payload = []
                        for sl in batch.subscription_line_ids:
                            sub = sl.subscription_id
                            if not sub:
                                continue
                            users = [
                                {
                                    "id": u.id,
                                    "name": u.name or "",
                                    "email": (u.partner_id.email if u.partner_id else "") or u.login or "",
                                }
                                for u in sl.assigned_user_ids
                            ]
                            subs_payload.append({
                                "subscription_id": sub.id,
                                "subscription_name": sub.name or "",
                                "cost_per_subscription": round(float(sl.cost_per_subscription or 0.0), 2),
                                "allocated_count": int(sl.subscription_count or 0),
                                "monthly_total": round(float(sl.final_amount or 0.0), 2),
                                "approved_amount": round(float(sl.approved_amount or 0.0), 2),
                                "per_day_cost": round(float(sl.per_day_cost or 0.0), 2),
                                "start_date": _fmt_date(sl.start_date),
                                "end_date": _fmt_date(sl.end_date),
                                "users": users,
                            })

                        batches_payload.append({
                            "batch_id": batch.id,
                            "batch_name": batch.name or "",
                            "batch_state": batch.state or "",
                            "start_date": _fmt_date(batch.start_date),
                            "end_date": _fmt_date(batch.end_date),
                            "days_elapsed": days,
                            "total_tasks": int(batch.total_tasks or 0),
                            "done_tasks": int(batch.done_tasks or 0),
                            "remaining_tasks": int(batch.remaining_tasks or 0),
                            **b_metrics,
                            "models": models_payload,
                            "infra": infra_payload,
                            "subscriptions": subs_payload,
                        })

                        proj_budget += b_budget
                        proj_est += b_est
                        proj_actual += b_actual
                        proj_run_rate += b_metrics["run_rate_per_day"]

                batches_payload.sort(
                    key=lambda x: (x["start_date"] or "", x["batch_id"]), reverse=True,
                )

                p_variance = proj_est - proj_actual
                p_remaining = proj_budget - proj_actual
                p_util = (proj_actual / proj_budget * 100.0) if proj_budget else 0.0

                projects_payload.append({
                    "project_id": pid,
                    "project_name": proj.name or "",
                    "budget": round(proj_budget, 2),
                    "estimation": round(proj_est, 2),
                    "actual": round(proj_actual, 2),
                    "variance": round(p_variance, 2),
                    "remaining": round(p_remaining, 2),
                    "util_pct": round(p_util, 2),
                    "run_rate_per_day": round(proj_run_rate, 2),
                    "batch_count": len(batches_payload),
                    "batches": batches_payload,
                })

                totals["budget"] += proj_budget
                totals["estimation"] += proj_est
                totals["actual"] += proj_actual
                totals["run_rate_per_day"] += proj_run_rate

            projects_payload.sort(key=lambda x: -x["util_pct"])

            t_variance = totals["estimation"] - totals["actual"]
            t_remaining = totals["budget"] - totals["actual"]
            t_util = (totals["actual"] / totals["budget"] * 100.0) if totals["budget"] else 0.0

            currency = _portfolio_currency(budgets)
            payload = {
                "window": {
                    "start": _fmt_date(start),
                    "end": _fmt_date(end),
                },
                "filters": {
                    "project_ids": project_ids,
                    "include_inactive": bool(jdata.get("include_inactive")),
                },
                "currency": currency["currency"],
                "currency_symbol": currency["currency_symbol"],
                "totals": {
                    "project_count": len(projects_payload),
                    "budget": round(totals["budget"], 2),
                    "estimation": round(totals["estimation"], 2),
                    "actual": round(totals["actual"], 2),
                    "variance": round(t_variance, 2),
                    "remaining": round(t_remaining, 2),
                    "util_pct": round(t_util, 2),
                    "run_rate_per_day": round(totals["run_rate_per_day"], 2),
                },
                "projects": projects_payload,
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("budget.project_budget_dashboard.project_summary failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )


    # ------------------------------------------------------------------
    # Metric builders
    # ------------------------------------------------------------------
    def _phase_metrics(self, batch):
        """Full metric dict for a single phase (etp.batch.budget)."""
        # BUDGET — sum of AWS / LLM cost lines matched to the phase.
        budget = sum(batch.matched_cost_line_ids.mapped("amount_source"))
        # ESTIMATED — approved amount for the phase.
        estimated = batch.approved_amount or 0.0
        # ACTUAL — from the Daily Task Log (done-count total cost).
        daily = batch.daily_task_ids
        actual = sum(daily.mapped("total_cost"))
        # BUFFER — amount the buffer_pct adds on top of the raw estimate.
        buffer_amount = (batch.batch_budget or 0.0) - (batch.estimated_cost or 0.0)
        # VARIANCE — estimated minus actual (positive => under budget).
        variance = estimated - actual

        # Burn rate + forecast off the elapsed / total phase duration.
        today = fields.Date.context_today(batch)
        start, end = batch.start_date, batch.end_date
        total_days = 0
        if start and end and end >= start:
            total_days = (end - start).days + 1
        elapsed_days = 0
        if start:
            upper = min(today, end) if end else today
            if upper >= start:
                elapsed_days = (upper - start).days + 1
        burn_rate = (actual / elapsed_days) if elapsed_days else 0.0
        forecast = burn_rate * total_days if total_days else actual

        return {
            "budget": _round2(budget),
            "estimated": _round2(estimated),
            "actual": _round2(actual),
            "buffer": _round2(buffer_amount),
            "buffer_pct": _round2(batch.buffer_pct),
            "variance": _round2(variance),
            "variance_pct": _pct(variance, estimated),
            "consumed_pct": _pct(actual, estimated),
            "actual_burn_rate": _round2(burn_rate),
            "forecast": _round2(forecast),
            "predicted_variance": _round2(forecast - estimated),
            "health": batch.health_status or "unknown",
            "done_tasks": batch.done_tasks or 0,
            "total_tasks": batch.total_tasks or 0,
            "remaining_tasks": batch.remaining_tasks or 0,
            "elapsed_days": elapsed_days,
            "total_days": total_days,
        }

    def _phase_summary(self, batch):
        """Compact phase row for the all-projects list."""
        m = self._phase_metrics(batch)
        m.update({
            "phase_id": batch.id,
            "phase_name": batch.name or "",
            "state": batch.state or "",
            "start_date": _date_str(batch.start_date),
            "end_date": _date_str(batch.end_date),
        })
        return m

    def _burn_series(self, batch, estimated=0.0):
        """Daily burn ``series`` for the "Daily spend — trend" chart, matching
        the dashboard chart convention (one point per day, ``date`` as
        ``%Y-%m-%d``). Each point carries:
          * ``spend``            — that day's spend (the daily bars/trend).
          * ``consumed_to_date`` — running total of actual spend (the purple
                                   burn curve that crosses the estimate).
          * ``estimated``        — the flat ESTIMATED reference (the red line).
          * ``done``             — done-task count for the day.
        """
        by_day = {}
        for log in batch.daily_task_ids.sorted("entry_date"):
            key = log.entry_date
            row = by_day.setdefault(key, {"spend": 0.0, "done": 0})
            row["spend"] += log.total_cost or 0.0
            row["done"] += log.done_count or 0
        series = []
        consumed_to_date = 0.0
        for day, v in sorted(by_day.items(),
                             key=lambda kv: (kv[0] or fields.Date.today())):
            consumed_to_date += v["spend"]
            series.append({
                "date": day.strftime("%Y-%m-%d") if day else "",
                "spend": _round2(v["spend"]),
                "consumed_to_date": _round2(consumed_to_date),
                "estimated": _round2(estimated),
                "done": v["done"],
            })
        return series

    @staticmethod
    def _sum_metric(rows, key):
        return _round2(sum(r.get(key, 0.0) for r in rows))

    # ------------------------------------------------------------------
    # API 1 — all projects with their phases (project -> budget -> phase)
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/etp_projects/project_performance",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_performance(self, **params):
        """List every project's budget performance, broken down by phase.

        Optional ?project_id=<int> to scope to a single project.
        """
        try:
            Budget = request.env[BUDGET_MODEL].sudo()
            domain = []
            if params.get("project_id"):
                try:
                    domain.append(("project_id", "=", int(params["project_id"])))
                except (TypeError, ValueError):
                    return return_Response(
                        message="'project_id' must be an integer.", status=400)

            budgets = Budget.search(domain, order="project_id, name")
            projects = []
            for pb in budgets:
                phases = [
                    self._phase_summary(ph)
                    for ph in pb.batch_budget_ids.sorted("start_date")
                ]
                projects.append({
                    "project_id": pb.project_id.id if pb.project_id else None,
                    "project_name": (pb.project_id.display_name
                                     if pb.project_id else ""),
                    "project_budget_id": pb.id,
                    "project_budget_name": pb.name or "",
                    "currency": (pb.currency_id.name
                                 if getattr(pb, "currency_id", False) else "USD"),
                    "totals": {
                        "budget": self._sum_metric(phases, "budget"),
                        "estimated": self._sum_metric(phases, "estimated"),
                        "actual": self._sum_metric(phases, "actual"),
                        "buffer": self._sum_metric(phases, "buffer"),
                        "variance": self._sum_metric(phases, "variance"),
                        "done_tasks": sum(p["done_tasks"] for p in phases),
                        "total_tasks": sum(p["total_tasks"] for p in phases),
                        "phase_count": len(phases),
                    },
                    "phases": phases,
                })

            return return_Response(
                message="OK", status=200, data={"data": {"projects": projects}})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("project_performance failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)])

    # ------------------------------------------------------------------
    # API 2 — one phase's detailed performance (pass ?phase_id=<int>)
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/etp_projects/project_performance/phase",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_performance_phase(self, **params):
        """Detailed performance for one phase, in its project context.

        ?phase_id=<int> (required) — the etp.batch.budget id selected in the UI.
        """
        raw = params.get("phase_id")
        try:
            phase_id = int(raw)
        except (TypeError, ValueError):
            return return_Response(
                message="'phase_id' query parameter is required and must be an "
                        "integer.", status=400)
        try:
            batch = request.env[BATCH_MODEL].sudo().browse(phase_id)
            if not batch.exists():
                return return_Response(
                    message="Phase %s not found." % phase_id, status=404)

            pb = batch.project_budget_id
            metrics = self._phase_metrics(batch)
            data = {
                "project_id": batch.project_id.id if batch.project_id else None,
                "project_name": (batch.project_id.display_name
                                 if batch.project_id else ""),
                "project_budget_id": pb.id if pb else None,
                "project_budget_name": pb.name if pb else "",
                "phase_id": batch.id,
                "phase_name": batch.name or "",
                "state": batch.state or "",
                "start_date": _date_str(batch.start_date),
                "end_date": _date_str(batch.end_date),
                "currency": (pb.currency_id.name
                             if pb and getattr(pb, "currency_id", False)
                             else "USD"),
                "metrics": metrics,
                # "Daily spend — trend" chart, following the dashboard chart
                # shape (title + window + series). The cumulative burn curve is
                # `series[].consumed_to_date`; the red reference line is
                # `estimated` (also on every point for easy plotting).
                "graph": {
                    "title": "Daily spend — trend",
                    "window": {
                        "start": _date_str(batch.start_date),
                        "end": _date_str(batch.end_date),
                    },
                    "estimated": metrics["estimated"],   # red reference line
                    "budget": metrics["budget"],
                    "actual": metrics["actual"],
                    "forecast": metrics["forecast"],
                    "series": self._burn_series(
                        batch, estimated=metrics["estimated"]),
                },
                # Sibling phases so the UI can switch between phases of the same
                # project without a second round-trip.
                "phases": [
                    {"phase_id": p.id, "phase_name": p.name or "",
                     "state": p.state or "",
                     "is_selected": p.id == batch.id}
                    for p in (pb.batch_budget_ids.sorted("start_date")
                              if pb else batch)
                ],
            }
            return return_Response(message="OK", status=200, data={"data": data})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("project_performance_phase failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)])