import logging
from collections import defaultdict

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
    _budget_domain,
    _coerce_float,
    _coerce_id_list,
    _portfolio_currency,
    _read_json_body,
)

_logger = logging.getLogger(__name__)

DEFAULT_ATTENTION_THRESHOLD_PCT = 80.0


def _round2(value):
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _share_pct(part, whole):
    if not whole:
        return 0.0
    return _round2((float(part) / float(whole)) * 100.0)


class EtpProjectsBudgetConsolidationController(http.Controller):

    @http.route(
        "/api/v1/etp_projects/budget_consolidation",
        methods=["POST"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
    )
    @validate_token
    def budget_consolidation(self, **kwargs):
        try:
            jdata = _read_json_body()

            project_ids = (
                _coerce_id_list(jdata.get("project_ids"), "project_ids")
                if jdata.get("project_ids") is not None
                else None
            )
            include_inactive = bool(jdata.get("include_inactive"))
            if "needs_attention_threshold_pct" in jdata:
                threshold_pct = _coerce_float(
                    jdata.get("needs_attention_threshold_pct"),
                    "needs_attention_threshold_pct",
                )
            else:
                threshold_pct = DEFAULT_ATTENTION_THRESHOLD_PCT

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(_budget_domain(project_ids or None, include_inactive))

            currency = _portfolio_currency(budgets)

            project_rows = {}
            for b in budgets:
                pid = b.project_id.id
                row = project_rows.get(pid)
                if not row:
                    row = {
                        "project_id": pid,
                        "project_name": b.project_id.display_name or "",
                        "budget": 0.0,
                        "spend": 0.0,
                        "burn": 0.0,
                    }
                    project_rows[pid] = row
                row["budget"] += float(b.budget_amount or 0.0)
                row["spend"] += float(b.total_consumed or 0.0)
                row["burn"] += float(b.daily_burn_rate or 0.0)

            total_spend = sum(r["spend"] for r in project_rows.values())
            total_budget = sum(r["budget"] for r in project_rows.values())
            total_remaining = total_budget - total_spend
            active_project_count = len(project_rows)

            needs_attention_count = 0
            for r in project_rows.values():
                pct = (r["spend"] / r["budget"] * 100.0) if r["budget"] else 0.0
                if pct >= threshold_pct:
                    needs_attention_count += 1

            per_project_model_spend = {pid: {} for pid in project_rows.keys()}
            model_totals = defaultdict(float)
            if project_rows:
                CostLine = request.env[COST_LINE_MODEL].sudo()
                cost_domain = [("project_id", "in", list(project_rows.keys()))]
                if not include_inactive:
                    cost_domain.append(("budget_id.active", "=", True))
                cost_groups = CostLine._read_group(
                    cost_domain,
                    groupby=["project_id", "service_name"],
                    aggregates=["amount_inr:sum"],
                )
                for project_rec, service, amt in cost_groups:
                    pid = project_rec.id
                    svc = service or NO_MODEL_LABEL
                    amount = float(amt or 0.0)
                    if not amount:
                        continue
                    bucket = per_project_model_spend.setdefault(pid, {})
                    bucket[svc] = bucket.get(svc, 0.0) + amount
                    model_totals[svc] += amount

            top_model = None
            if model_totals:
                tn, ta = max(model_totals.items(), key=lambda x: x[1])
                top_model = {
                    "model_name": tn,
                    "amount": _round2(ta),
                    "share_pct": _share_pct(ta, total_spend),
                }

            spend_by_model = sorted(
                [
                    {
                        "model_name": m,
                        "amount": _round2(a),
                        "share_pct": _share_pct(a, total_spend),
                    }
                    for m, a in model_totals.items()
                ],
                key=lambda x: x["amount"],
                reverse=True,
            )

            spend_by_project = sorted(
                [
                    {
                        "project_id": r["project_id"],
                        "project_name": r["project_name"],
                        "amount": _round2(r["spend"]),
                        "share_pct": _share_pct(r["spend"], total_spend),
                    }
                    for r in project_rows.values()
                ],
                key=lambda x: x["amount"],
                reverse=True,
            )

            budget_by_projects = []
            for pid, row in project_rows.items():
                attr = per_project_model_spend.get(pid, {})
                proj_attr_total = sum(attr.values())
                models_list = sorted(
                    [
                        {
                            "model_name": m,
                            "spend": _round2(a),
                            "share_pct": _share_pct(a, proj_attr_total),
                        }
                        for m, a in attr.items()
                    ],
                    key=lambda x: x["spend"],
                    reverse=True,
                )
                top_m = models_list[0]["model_name"] if models_list else None
                util_pct = (row["spend"] / row["budget"] * 100.0) if row["budget"] else 0.0
                runway_days = None
                if row["burn"] > 0:
                    runway_days = int(max(0.0, (row["budget"] - row["spend"]) / row["burn"]))
                budget_by_projects.append(
                    {
                        "project_id": row["project_id"],
                        "project_name": row["project_name"],
                        "spend": _round2(row["spend"]),
                        "budget": _round2(row["budget"]),
                        "remaining": _round2(row["budget"] - row["spend"]),
                        "utilization_pct": _round2(util_pct),
                        "runway_days": runway_days,
                        "top_model": top_m,
                        "models": models_list,
                    }
                )
            budget_by_projects.sort(key=lambda x: x["utilization_pct"], reverse=True)

            payload = {
                "kpis": {
                    "total_spend": _round2(total_spend),
                    "total_remaining": _round2(total_remaining),
                    "percent_consumed": _round2(
                        (total_spend / total_budget * 100.0) if total_budget else 0.0
                    ),
                    "total_budget": _round2(total_budget),
                    "active_project_count": active_project_count,
                    "needs_attention_count": needs_attention_count,
                    "needs_attention_threshold_pct": _round2(threshold_pct),
                    "top_model": top_model,
                    "currency": currency.get("currency"),
                    "currency_symbol": currency.get("currency_symbol"),
                },
                "spend_by_model": spend_by_model,
                "spend_by_project": spend_by_project,
                "budget_by_projects": budget_by_projects,
            }
            return return_Response(message="OK", status=200, data={"data": payload})

        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("budget_consolidation failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)]
            )
