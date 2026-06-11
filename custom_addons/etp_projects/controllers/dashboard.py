import calendar
import json
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

_logger = logging.getLogger(__name__)

BUDGET_MODEL = "etp.project.aws.budget"
REQUEST_MODEL = "etp.project.token.purchase.request"
COST_LINE_MODEL = "etp.project.aws.cost.line"

NO_MODEL_LABEL = "(no model)"
DEFAULT_HEALTH_THRESHOLDS = (75.0, 90.0)
DEFAULT_TIMELINE_DAYS = 30
DEFAULT_DAILY_BURN_DAYS = 30


def _read_json_body():
    raw = b""
    try:
        raw = request.httprequest.stream.read() or b""
    except Exception:
        raw = request.httprequest.data or b""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _coerce_int(value, field_name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError("'%s' must be an integer." % field_name)


def _coerce_float(value, field_name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError("'%s' must be a number." % field_name)


def _coerce_date(value, field_name):
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass
    raise ValidationError("'%s' must be a date in YYYY-MM-DD format." % field_name)


def _coerce_id_list(value, field_name):
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(x, int) for x in value):
        raise ValidationError("'%s' must be a list of integers." % field_name)
    return value


def _budget_domain(project_ids=None, include_inactive=False):
    domain = []
    if not include_inactive:
        domain.append(("active", "=", True))
    if project_ids:
        domain.append(("project_id", "in", project_ids))
    return domain


def _days_in_next_month(today=None):
    today = today or date.today()
    next_month_start = today.replace(day=1) + relativedelta(months=1)
    next_next = next_month_start + relativedelta(months=1)
    return (next_next - next_month_start).days


def _health(percent_consumed, thresholds=DEFAULT_HEALTH_THRESHOLDS):
    green_max, amber_max = thresholds
    if percent_consumed < green_max:
        return "green"
    if percent_consumed < amber_max:
        return "amber"
    return "red"


def _portfolio_currency(budgets):
    cur = None
    for b in budgets:
        if b.currency_id:
            cur = b.currency_id
            break
    if cur:
        return {"currency": cur.name or "", "currency_symbol": cur.symbol or ""}
    inr = request.env.ref("base.INR", raise_if_not_found=False)
    if inr:
        return {"currency": inr.name or "INR", "currency_symbol": inr.symbol or "\u20b9"}
    return {"currency": "INR", "currency_symbol": "\u20b9"}


def _currency_of(budget):
    cur = budget.currency_id
    return {
        "currency": cur.name if cur else "",
        "currency_symbol": cur.symbol if cur else "",
    }


def _project_pair(budget):
    return {
        "project_id": budget.project_id.id if budget.project_id else False,
        "project_name": budget.project_id.name if budget.project_id else "",
    }


def _parse_window(jdata, default_days):
    start = _coerce_date(jdata.get("start"), "start")
    end = _coerce_date(jdata.get("end"), "end")
    if not end:
        end = date.today()
    if not start:
        graph_days = jdata.get("graph_days") or default_days
        try:
            graph_days = int(graph_days)
        except (TypeError, ValueError):
            graph_days = default_days
        graph_days = max(1, min(graph_days, 365))
        start = end - timedelta(days=graph_days - 1)
    if start > end:
        raise ValidationError("'start' must be on or before 'end'.")
    return start, end


def _get_budgets(jdata):
    project_ids = _coerce_id_list(jdata.get("project_ids"), "project_ids")
    include_inactive = bool(jdata.get("include_inactive"))
    Budget = request.env[BUDGET_MODEL].sudo()
    return Budget.search(_budget_domain(project_ids, include_inactive)), project_ids


def _completed_tpr_domain(project_ids=None, completed_after=None, completed_before=None):
    domain = [("state", "=", "completed"), ("completed_date", "!=", False)]
    if project_ids:
        domain.append(("project_id", "in", project_ids))
    if completed_after:
        domain.append(("completed_date", ">=", completed_after))
    if completed_before:
        domain.append(("completed_date", "<=", completed_before))
    return domain


class EtpProjectsDashboardController(http.Controller):

    @http.route(
        "/api/v1/etp_projects/dashboard/kpis",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def kpis(self, **params):
        try:
            jdata = _read_json_body()
            budgets, _project_ids = _get_budgets(jdata)

            total_budget = sum(b.budget_amount or 0.0 for b in budgets)
            total_spend = sum(b.total_consumed or 0.0 for b in budgets)
            total_remaining = sum(b.remaining or 0.0 for b in budgets)
            total_burn = sum(b.daily_burn_rate or 0.0 for b in budgets)
            percent_consumed = (total_spend / total_budget * 100.0) if total_budget else 0.0
            runway_days = (total_remaining / total_burn) if total_burn else None
            forecast = total_burn * _days_in_next_month()

            payload = _portfolio_currency(budgets)
            payload.update({
                "total_spend": round(total_spend, 2),
                "total_project_budget": round(total_budget, 2),
                "total_remaining": round(total_remaining, 2),
                "percent_consumed": round(percent_consumed, 2),
                "daily_burn_rate": round(total_burn, 2),
                "runway_days": round(runway_days, 1) if runway_days is not None else None,
                "forecast_next_month": round(forecast, 2),
                "budget_count": len(budgets),
                "project_count": len({b.project_id.id for b in budgets if b.project_id}),
            })
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.kpis failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/budget_timeline",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def budget_timeline(self, **params):
        try:
            jdata = _read_json_body()
            single_project_id = jdata.get("project_id")
            if single_project_id is not None:
                pid = _coerce_int(single_project_id, "project_id")
                start = _coerce_date(jdata.get("start"), "start")
                end = _coerce_date(jdata.get("end"), "end")
                graph_days = jdata.get("graph_days") or DEFAULT_TIMELINE_DAYS
                try:
                    graph_days = int(graph_days)
                except (TypeError, ValueError):
                    graph_days = DEFAULT_TIMELINE_DAYS
                payload = request.env[REQUEST_MODEL].sudo()._get_budget_timeline_for_project(
                    pid, start=start, end=end, graph_days=graph_days,
                )
                return return_Response(message="OK", status=200, data={"data": payload})

            project_ids = _coerce_id_list(jdata.get("project_ids"), "project_ids")
            include_inactive = bool(jdata.get("include_inactive"))
            start, end = _parse_window(jdata, DEFAULT_TIMELINE_DAYS)

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(_budget_domain(project_ids, include_inactive))
            budget_ids = budgets.ids

            TPR = request.env[REQUEST_MODEL].sudo()
            CL = request.env[COST_LINE_MODEL].sudo()

            tprs = TPR.search([
                ("budget_id", "in", budget_ids),
                ("state", "=", "completed"),
                ("completed_date", "!=", False),
            ])
            cls = CL.search([("budget_id", "in", budget_ids)])

            pre_added = 0.0
            tpr_by_day = defaultdict(float)
            for t in tprs:
                d = t.completed_date.date()
                amt = float(t.approved_amount or 0.0)
                if d < start:
                    pre_added += amt
                elif d <= end:
                    tpr_by_day[d] += amt

            pre_consumed = 0.0
            consumed_by_day = defaultdict(float)
            for cl in cls:
                period = cl.period
                if not period:
                    continue
                dim = calendar.monthrange(period.year, period.month)[1]
                amt = float(cl.amount_inr or 0.0)
                daily = amt / dim if dim else 0.0
                for i in range(dim):
                    d = period + timedelta(days=i)
                    if d < start:
                        pre_consumed += daily
                    elif d <= end:
                        consumed_by_day[d] += daily

            cumulative_added = pre_added
            cumulative_consumed = pre_consumed
            last_topup_consumed = 0.0
            series = []
            d = start
            while d <= end:
                add_today = tpr_by_day.get(d, 0.0)
                consume_today = consumed_by_day.get(d, 0.0)
                cumulative_added += add_today
                cumulative_consumed += consume_today
                available = cumulative_added - cumulative_consumed
                if add_today > 0:
                    event = {
                        "type": "top_up",
                        "label": "+%.2f" % add_today,
                        "added": round(add_today, 2),
                        "available_after": round(available, 2),
                        "spent_since_last_topup": round(last_topup_consumed, 2),
                    }
                    last_topup_consumed = 0.0
                else:
                    event = {}
                    last_topup_consumed += consume_today
                series.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "available_balance": round(available, 2),
                    "consumed_to_date": round(cumulative_consumed, 2),
                    "added_to_date": round(cumulative_added, 2),
                    "event": event,
                })
                d += timedelta(days=1)

            cur = _portfolio_currency(budgets)
            payload = {
                "title": "Funded vs Remaining Balance",
                "range": "%dd" % ((end - start).days + 1),
                "available_now": series[-1]["available_balance"] if series else 0.0,
                "window": {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")},
                "series": series,
                "currency": cur["currency"],
                "currency_symbol": cur["currency_symbol"],
                "budget_count": len(budgets),
                "project_count": len({b.project_id.id for b in budgets if b.project_id}),
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.budget_timeline failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/daily_burn",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def daily_burn(self, **params):
        try:
            jdata = _read_json_body()
            project_ids = _coerce_id_list(jdata.get("project_ids"), "project_ids")
            include_inactive = bool(jdata.get("include_inactive"))
            start, end = _parse_window(jdata, DEFAULT_DAILY_BURN_DAYS)

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(_budget_domain(project_ids, include_inactive))
            budget_ids = budgets.ids

            CL = request.env[COST_LINE_MODEL].sudo()
            cls = CL.search([
                ("budget_id", "in", budget_ids),
                ("period", ">=", start.replace(day=1)),
                ("period", "<=", end),
            ])

            per_day = defaultdict(lambda: defaultdict(float))
            for cl in cls:
                period = cl.period
                if not period:
                    continue
                dim = calendar.monthrange(period.year, period.month)[1]
                amt = float(cl.amount_inr or 0.0)
                daily = amt / dim if dim else 0.0
                svc = cl.service_name or "Other"
                for i in range(dim):
                    d = period + timedelta(days=i)
                    if start <= d <= end:
                        per_day[d][svc] += daily

            services = sorted({svc for day_map in per_day.values() for svc in day_map.keys()})
            series = []
            d = start
            while d <= end:
                day_map = per_day.get(d, {})
                total = sum(day_map.values())
                series.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "total_inr": round(total, 2),
                    "by_service": [
                        {"service_name": s, "amount": round(day_map.get(s, 0.0), 2)}
                        for s in services
                    ],
                })
                d += timedelta(days=1)

            avg_per_day = (sum(s["total_inr"] for s in series) / len(series)) if series else 0.0
            cur = _portfolio_currency(budgets)
            payload = {
                "title": "Daily Burn",
                "window": {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")},
                "services": services,
                "avg_per_day": round(avg_per_day, 2),
                "series": series,
                "currency": cur["currency"],
                "currency_symbol": cur["currency_symbol"],
                "note": "Monthly cost-line totals spread evenly across calendar days of each period.",
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.daily_burn failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/spend_by_model",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def spend_by_model(self, **params):
        try:
            jdata = _read_json_body()
            project_ids = _coerce_id_list(jdata.get("project_ids"), "project_ids")
            start = _coerce_date(jdata.get("start"), "start")
            end = _coerce_date(jdata.get("end"), "end")
            after = datetime.combine(start, datetime.min.time()) if start else None
            before = datetime.combine(end, datetime.max.time()) if end else None

            TPR = request.env[REQUEST_MODEL].sudo()
            tprs = TPR.search(_completed_tpr_domain(project_ids, after, before))

            by_model = defaultdict(lambda: {"total": 0.0, "count": 0})
            for t in tprs:
                m = t.model_name or NO_MODEL_LABEL
                by_model[m]["total"] += float(t.approved_amount or 0.0)
                by_model[m]["count"] += 1

            bars = [
                {
                    "model_name": m,
                    "total": round(d["total"], 2),
                    "request_count": d["count"],
                    "avg_per_request": round(d["total"] / d["count"], 2) if d["count"] else 0.0,
                }
                for m, d in sorted(by_model.items(), key=lambda kv: -kv[1]["total"])
            ]
            total = round(sum(b["total"] for b in bars), 2)

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(_budget_domain(project_ids))
            cur = _portfolio_currency(budgets)

            payload = {
                "title": "Spend By Model",
                "window": {
                    "start": start.strftime("%Y-%m-%d") if start else "",
                    "end": end.strftime("%Y-%m-%d") if end else "",
                },
                "total_spend": total,
                "bars": bars,
                "currency": cur["currency"],
                "currency_symbol": cur["currency_symbol"],
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.spend_by_model failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/model_cost_table",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def model_cost_table(self, **params):
        try:
            jdata = _read_json_body()
            project_ids = _coerce_id_list(jdata.get("project_ids"), "project_ids")
            start = _coerce_date(jdata.get("start"), "start")
            end = _coerce_date(jdata.get("end"), "end")
            after = datetime.combine(start, datetime.min.time()) if start else None
            before = datetime.combine(end, datetime.max.time()) if end else None

            TPR = request.env[REQUEST_MODEL].sudo()
            tprs = TPR.search(_completed_tpr_domain(project_ids, after, before))

            by_model = defaultdict(lambda: {"total": 0.0, "count": 0})
            for t in tprs:
                m = t.model_name or NO_MODEL_LABEL
                by_model[m]["total"] += float(t.approved_amount or 0.0)
                by_model[m]["count"] += 1

            total = sum(d["total"] for d in by_model.values())

            rows = []
            for m, d in sorted(by_model.items(), key=lambda kv: -kv[1]["total"]):
                rows.append({
                    "model_name": m,
                    "spend": round(d["total"], 2),
                    "share_pct": round(d["total"] / total * 100.0, 2) if total else 0.0,
                    "request_count": d["count"],
                    "avg_per_request": round(d["total"] / d["count"], 2) if d["count"] else 0.0,
                    "token_in": None,
                    "token_out": None,
                    "blended_rate": None,
                    "cost": round(d["total"], 2),
                })

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(_budget_domain(project_ids))
            cur = _portfolio_currency(budgets)

            payload = {
                "title": "Model Cost Consumption",
                "window": {
                    "start": start.strftime("%Y-%m-%d") if start else "",
                    "end": end.strftime("%Y-%m-%d") if end else "",
                },
                "total_spend": round(total, 2),
                "rows": rows,
                "currency": cur["currency"],
                "currency_symbol": cur["currency_symbol"],
                "notes": {
                    "token_in": "Not tracked in etp_projects.",
                    "token_out": "Not tracked in etp_projects.",
                    "blended_rate": "Not tracked in etp_projects.",
                },
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.model_cost_table failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/budget_utilization_by_project",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def budget_utilization_by_project(self, **params):
        try:
            jdata = _read_json_body()
            budgets, _project_ids = _get_budgets(jdata)

            rows = []
            for b in budgets:
                row = _project_pair(b)
                row.update(_currency_of(b))
                row.update({
                    "budget_id": b.id,
                    "budget_name": b.name or "",
                    "budget_amount": float(b.budget_amount or 0.0),
                    "total_consumed": float(b.total_consumed or 0.0),
                    "remaining": float(b.remaining or 0.0),
                    "percent_consumed": round(float(b.percent_consumed or 0.0), 2),
                    "health": _health(float(b.percent_consumed or 0.0)),
                })
                rows.append(row)

            rows.sort(key=lambda r: -r["percent_consumed"])
            payload = {
                "title": "Budget Utilization by Project",
                "total_rows": len(rows),
                "rows": rows,
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.budget_utilization_by_project failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/project_model_consumption",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_model_consumption(self, **params):
        try:
            jdata = _read_json_body()
            project_ids = _coerce_id_list(jdata.get("project_ids"), "project_ids")
            start = _coerce_date(jdata.get("start"), "start")
            end = _coerce_date(jdata.get("end"), "end")
            after = datetime.combine(start, datetime.min.time()) if start else None
            before = datetime.combine(end, datetime.max.time()) if end else None

            TPR = request.env[REQUEST_MODEL].sudo()
            tprs = TPR.search(_completed_tpr_domain(project_ids, after, before))

            by_pm = defaultdict(lambda: defaultdict(lambda: {"total": 0.0, "count": 0}))
            proj_names = {}
            for t in tprs:
                pid = t.project_id.id if t.project_id else 0
                proj_names[pid] = t.project_id.name if t.project_id else ""
                m = t.model_name or NO_MODEL_LABEL
                by_pm[pid][m]["total"] += float(t.approved_amount or 0.0)
                by_pm[pid][m]["count"] += 1

            projects = []
            for pid, model_dict in by_pm.items():
                models = [
                    {
                        "model_name": m,
                        "total": round(d["total"], 2),
                        "request_count": d["count"],
                    }
                    for m, d in sorted(model_dict.items(), key=lambda kv: -kv[1]["total"])
                ]
                project_total = sum(m["total"] for m in models)
                for m in models:
                    m["share_pct"] = round(m["total"] / project_total * 100.0, 2) if project_total else 0.0
                projects.append({
                    "project_id": pid,
                    "project_name": proj_names.get(pid, ""),
                    "total": round(project_total, 2),
                    "models": models,
                })
            projects.sort(key=lambda p: -p["total"])

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(_budget_domain(project_ids))
            cur = _portfolio_currency(budgets)

            payload = {
                "title": "Project-wise Model Consumption",
                "window": {
                    "start": start.strftime("%Y-%m-%d") if start else "",
                    "end": end.strftime("%Y-%m-%d") if end else "",
                },
                "projects": projects,
                "currency": cur["currency"],
                "currency_symbol": cur["currency_symbol"],
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.project_model_consumption failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/project_forecast_vs_funded",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_forecast_vs_funded(self, **params):
        try:
            jdata = _read_json_body()
            budgets, _project_ids = _get_budgets(jdata)

            days_next = _days_in_next_month()
            rows = []
            for b in budgets:
                burn = float(b.daily_burn_rate or 0.0)
                forecast = burn * days_next
                remaining = float(b.remaining or 0.0)
                row = _project_pair(b)
                row.update(_currency_of(b))
                row.update({
                    "budget_id": b.id,
                    "funded": float(b.budget_amount or 0.0),
                    "consumed": float(b.total_consumed or 0.0),
                    "remaining": remaining,
                    "forecast_next_month": round(forecast, 2),
                    "variance": round(forecast - remaining, 2),
                    "daily_burn_rate": round(burn, 2),
                })
                rows.append(row)
            rows.sort(key=lambda r: -r["variance"])

            payload = {
                "title": "Project Forecast vs Funded",
                "days_in_next_month": days_next,
                "rows": rows,
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.project_forecast_vs_funded failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/efficiency_cost_per_output",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def efficiency_cost_per_output(self, **params):
        try:
            jdata = _read_json_body()
            budgets, _project_ids = _get_budgets(jdata)

            project_id_list = [b.project_id.id for b in budgets if b.project_id]
            TPR = request.env[REQUEST_MODEL].sudo()
            tprs = TPR.search(_completed_tpr_domain(project_id_list)) if project_id_list else TPR.browse([])

            count_by_proj = defaultdict(int)
            for t in tprs:
                if t.project_id:
                    count_by_proj[t.project_id.id] += 1

            rows = []
            for b in budgets:
                pid = b.project_id.id if b.project_id else 0
                count = count_by_proj.get(pid, 0)
                cost = float(b.total_consumed or 0.0)
                cpr = round(cost / count, 2) if count else None
                row = _project_pair(b)
                row.update(_currency_of(b))
                row.update({
                    "budget_id": b.id,
                    "total_consumed": round(cost, 2),
                    "successful_outputs": count,
                    "cost_per_output": cpr,
                })
                rows.append(row)
            rows.sort(key=lambda r: (-(r["cost_per_output"] or 0.0)))

            payload = {
                "title": "Efficiency: Cost per Successful Output",
                "rows": rows,
                "note": "Successful output = completed token purchase request (TPR). No task-level success tracking in etp_projects.",
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.efficiency_cost_per_output failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/spend_bridge",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def spend_bridge(self, **params):
        try:
            jdata = _read_json_body()
            project_ids = _coerce_id_list(jdata.get("project_ids"), "project_ids")
            period_a_start = _coerce_date(jdata.get("period_a_start"), "period_a_start")
            period_a_end = _coerce_date(jdata.get("period_a_end"), "period_a_end")
            period_b_start = _coerce_date(jdata.get("period_b_start"), "period_b_start")
            period_b_end = _coerce_date(jdata.get("period_b_end"), "period_b_end")

            today = date.today()
            if not period_b_end:
                period_b_end = today
            if not period_b_start:
                period_b_start = period_b_end.replace(day=1)
            if not period_a_end:
                period_a_end = period_b_start - timedelta(days=1)
            if not period_a_start:
                period_a_start = period_a_end.replace(day=1)

            if period_a_start > period_a_end or period_b_start > period_b_end:
                raise ValidationError("Period start must be on or before period end.")

            TPR = request.env[REQUEST_MODEL].sudo()

            def _fetch_period(p_start, p_end):
                after = datetime.combine(p_start, datetime.min.time())
                before = datetime.combine(p_end, datetime.max.time())
                tprs = TPR.search(_completed_tpr_domain(project_ids, after, before))
                acc = defaultdict(lambda: {"total": 0.0, "count": 0})
                for t in tprs:
                    m = t.model_name or NO_MODEL_LABEL
                    acc[m]["total"] += float(t.approved_amount or 0.0)
                    acc[m]["count"] += 1
                return acc

            by_a = _fetch_period(period_a_start, period_a_end)
            by_b = _fetch_period(period_b_start, period_b_end)

            prev = sum(d["total"] for d in by_a.values())
            now = sum(d["total"] for d in by_b.values())

            per_model = []
            volume_total = 0.0
            rate_total = 0.0
            for m in sorted(set(by_a) | set(by_b)):
                ca = by_a.get(m, {"total": 0.0, "count": 0})
                cb = by_b.get(m, {"total": 0.0, "count": 0})
                avg_a = (ca["total"] / ca["count"]) if ca["count"] else 0.0
                avg_b = (cb["total"] / cb["count"]) if cb["count"] else 0.0
                volume_m = (cb["count"] - ca["count"]) * avg_a
                rate_m = cb["count"] * (avg_b - avg_a)
                per_model.append({
                    "model_name": m,
                    "prev": round(ca["total"], 2),
                    "now": round(cb["total"], 2),
                    "delta": round(cb["total"] - ca["total"], 2),
                    "volume_effect": round(volume_m, 2),
                    "rate_effect": round(rate_m, 2),
                })
                volume_total += volume_m
                rate_total += rate_m

            mix = now - prev - volume_total - rate_total

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(_budget_domain(project_ids))
            cur = _portfolio_currency(budgets)

            bridge = [
                {"label": "Previous (A)", "value": round(prev, 2),
                 "cumulative": round(prev, 2), "type": "anchor"},
                {"label": "Volume", "value": round(volume_total, 2),
                 "cumulative": round(prev + volume_total, 2), "type": "delta"},
                {"label": "Model Mix", "value": round(mix, 2),
                 "cumulative": round(prev + volume_total + mix, 2), "type": "delta"},
                {"label": "Rate", "value": round(rate_total, 2),
                 "cumulative": round(prev + volume_total + mix + rate_total, 2), "type": "delta"},
                {"label": "Now (B)", "value": round(now, 2),
                 "cumulative": round(now, 2), "type": "anchor"},
            ]
            payload = {
                "title": "Spend Bridge: Why spend moved",
                "period_a": {
                    "start": period_a_start.strftime("%Y-%m-%d"),
                    "end": period_a_end.strftime("%Y-%m-%d"),
                    "total": round(prev, 2),
                },
                "period_b": {
                    "start": period_b_start.strftime("%Y-%m-%d"),
                    "end": period_b_end.strftime("%Y-%m-%d"),
                    "total": round(now, 2),
                },
                "bridge": bridge,
                "per_model": per_model,
                "currency": cur["currency"],
                "currency_symbol": cur["currency_symbol"],
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.spend_bridge failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/period_over_period_by_model",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def period_over_period_by_model(self, **params):
        try:
            jdata = _read_json_body()
            project_ids = _coerce_id_list(jdata.get("project_ids"), "project_ids")
            periods_input = jdata.get("periods")
            today = date.today()
            periods = []
            if not periods_input:
                for offset in (2, 1, 0):
                    month_start = (today.replace(day=1) - relativedelta(months=offset))
                    month_end = (month_start + relativedelta(months=1)) - timedelta(days=1)
                    periods.append({
                        "label": month_start.strftime("%Y-%m"),
                        "start": month_start,
                        "end": month_end,
                    })
            else:
                if not isinstance(periods_input, list):
                    raise ValidationError("'periods' must be a list.")
                for idx, p in enumerate(periods_input):
                    if not isinstance(p, dict):
                        raise ValidationError("'periods[%d]' must be an object." % idx)
                    pstart = _coerce_date(p.get("start"), "periods[%d].start" % idx)
                    pend = _coerce_date(p.get("end"), "periods[%d].end" % idx)
                    if not pstart or not pend:
                        raise ValidationError("'periods[%d]' requires start and end." % idx)
                    if pstart > pend:
                        raise ValidationError("'periods[%d]' start must be <= end." % idx)
                    label = (p.get("label") or "").strip() or pstart.strftime("%Y-%m-%d")
                    periods.append({"label": label, "start": pstart, "end": pend})

            TPR = request.env[REQUEST_MODEL].sudo()
            all_models = set()
            for p in periods:
                after = datetime.combine(p["start"], datetime.min.time())
                before = datetime.combine(p["end"], datetime.max.time())
                tprs = TPR.search(_completed_tpr_domain(project_ids, after, before))
                by_model = defaultdict(float)
                for t in tprs:
                    m = t.model_name or NO_MODEL_LABEL
                    by_model[m] += float(t.approved_amount or 0.0)
                    all_models.add(m)
                p["by_model"] = dict(by_model)
                p["total"] = sum(by_model.values())

            rows = []
            for m in sorted(all_models):
                cells = []
                for p in periods:
                    cells.append({
                        "period_label": p["label"],
                        "amount": round(p["by_model"].get(m, 0.0), 2),
                    })
                row_total = sum(c["amount"] for c in cells)
                rows.append({"model_name": m, "total": round(row_total, 2), "cells": cells})
            rows.sort(key=lambda r: -r["total"])

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(_budget_domain(project_ids))
            cur = _portfolio_currency(budgets)

            payload = {
                "title": "Period over Period by Model",
                "periods": [
                    {
                        "label": p["label"],
                        "start": p["start"].strftime("%Y-%m-%d"),
                        "end": p["end"].strftime("%Y-%m-%d"),
                        "total": round(p["total"], 2),
                    }
                    for p in periods
                ],
                "rows": rows,
                "currency": cur["currency"],
                "currency_symbol": cur["currency_symbol"],
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.period_over_period_by_model failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/budget_movement",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def budget_movement(self, **params):
        try:
            jdata = _read_json_body()
            single_project_id = jdata.get("project_id")
            if single_project_id is not None:
                pid = _coerce_int(single_project_id, "project_id")
                payload = request.env[REQUEST_MODEL].sudo()._get_allocation_ledger_for_project(pid)
                return return_Response(message="OK", status=200, data={"data": payload})

            project_ids = _coerce_id_list(jdata.get("project_ids"), "project_ids")
            try:
                limit = int(jdata.get("limit") or 100)
            except (TypeError, ValueError):
                limit = 100
            try:
                offset = int(jdata.get("offset") or 0)
            except (TypeError, ValueError):
                offset = 0
            limit = max(1, min(limit, 500))
            offset = max(0, offset)

            domain = [("state", "=", "completed"), ("completed_date", "!=", False)]
            if project_ids:
                domain.append(("project_id", "in", project_ids))

            TPR = request.env[REQUEST_MODEL].sudo()
            total = TPR.search_count(domain)
            tprs = TPR.search(domain, order="completed_date desc, id desc", limit=limit, offset=offset)

            entries = []
            for t in tprs:
                entries.append({
                    "datetime": t.completed_date.strftime("%Y-%m-%dT%H:%M:%SZ") if t.completed_date else "",
                    "action": "top_up",
                    "action_label": "%s %s" % (
                        t.project_id.name if t.project_id else "",
                        t.name or "",
                    ),
                    "tpr_id": t.id,
                    "tpr_name": t.name or "",
                    "project_id": t.project_id.id if t.project_id else False,
                    "project_name": t.project_id.name if t.project_id else "",
                    "model_name": t.model_name or "",
                    "amount": float(t.approved_amount or 0.0),
                    "balance_before": float(t.balance_before or 0.0),
                })

            payload = {
                "title": "Allocation Ledger (Portfolio)",
                "total": total,
                "limit": limit,
                "offset": offset,
                "count": len(entries),
                "entries": entries,
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.budget_movement failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route(
        "/api/v1/etp_projects/dashboard/portfolio_drilldown",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def portfolio_drilldown(self, **params):
        try:
            jdata = _read_json_body()
            budgets, _project_ids = _get_budgets(jdata)

            project_id_list = [b.project_id.id for b in budgets if b.project_id]
            TPR = request.env[REQUEST_MODEL].sudo()
            tprs = TPR.search(_completed_tpr_domain(project_id_list)) if project_id_list else TPR.browse([])

            by_proj_model = defaultdict(lambda: defaultdict(lambda: {"total": 0.0, "count": 0}))
            for t in tprs:
                pid = t.project_id.id if t.project_id else 0
                m = t.model_name or NO_MODEL_LABEL
                by_proj_model[pid][m]["total"] += float(t.approved_amount or 0.0)
                by_proj_model[pid][m]["count"] += 1

            days_next = _days_in_next_month()
            rows = []
            for b in budgets:
                pid = b.project_id.id if b.project_id else 0
                burn = float(b.daily_burn_rate or 0.0)
                allocation = float(b.budget_amount or 0.0)
                spend = float(b.total_consumed or 0.0)
                util_pct = float(b.percent_consumed or 0.0)
                remaining = float(b.remaining or 0.0)
                runway = round(remaining / burn, 1) if burn else None
                forecast = burn * days_next
                variance = forecast - remaining

                model_dict = by_proj_model.get(pid, {})
                models = [
                    {
                        "model_name": m,
                        "total": round(d["total"], 2),
                        "request_count": d["count"],
                    }
                    for m, d in sorted(model_dict.items(), key=lambda kv: -kv[1]["total"])
                ]
                proj_total = sum(m["total"] for m in models)
                for m in models:
                    m["share_pct"] = round(m["total"] / proj_total * 100.0, 2) if proj_total else 0.0

                top_model = models[0]["model_name"] if models else ""
                top_attr = models[0]["share_pct"] if models else 0.0

                row = _project_pair(b)
                row.update(_currency_of(b))
                row.update({
                    "budget_id": b.id,
                    "budget_name": b.name or "",
                    "allocation": round(allocation, 2),
                    "spend": round(spend, 2),
                    "util_pct": round(util_pct, 2),
                    "remaining": round(remaining, 2),
                    "forecast_next_month": round(forecast, 2),
                    "variance": round(variance, 2),
                    "burn_rate_per_day": round(burn, 2),
                    "runway_days": runway,
                    "health": _health(util_pct),
                    "top_model": top_model,
                    "top_model_attr_pct": top_attr,
                    "models": models,
                })
                rows.append(row)

            rows.sort(key=lambda r: -r["util_pct"])

            payload = {
                "title": "Portfolio Budget Drilldown",
                "days_in_next_month": days_next,
                "rows": rows,
            }
            return return_Response(message="OK", status=200, data={"data": payload})
        except ValidationError as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("dashboard.portfolio_drilldown failed")
            return return_Response(message="Something went wrong.", status=400, errors=[str(e)])
