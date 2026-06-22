import base64
import io
import json
import logging
from datetime import date, datetime, timedelta

from odoo import fields, http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    generate_s3_link,
    return_Response,
    validate_token,
)

BUDGET_MODEL = 'etp.project.aws.budget'

_logger = logging.getLogger(__name__)


class EtpProjectsAwsCostController(http.Controller):

    def _read_json_body(self):
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

    @http.route(
        '/api/v1/etp_projects/aws_cost/status_all',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    def update_all_aws_cost(self, **params):
        try:
            jdata = self._read_json_body()

            include_inactive = bool(jdata.get('include_inactive'))
            domain = []
            if not include_inactive:
                domain.append(('active', '=', True))

            budget_ids = jdata.get('budget_ids') or []
            project_ids = jdata.get('project_ids') or []
            if budget_ids:
                if not isinstance(budget_ids, list) or not all(isinstance(x, int) for x in budget_ids):
                    return return_Response(
                        message="'budget_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('id', 'in', budget_ids))
            if project_ids:
                if not isinstance(project_ids, list) or not all(isinstance(x, int) for x in project_ids):
                    return return_Response(
                        message="'project_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('project_id', 'in', project_ids))

            Budget = request.env['etp.project.aws.budget'].sudo()
            budgets = Budget.search(domain)

            if not budgets:
                return return_Response(
                    message="No AWS budgets matched the given filters.",
                    status=200,
                    data={"data": {
                        "total_budgets": 0,
                        "success_count": 0,
                        "error_count": 0,
                        "total_created": 0,
                        "total_updated": 0,
                        "total_api_hits": 0,
                        "total_api_hit_cost_usd": 0.0,
                        "results": [],
                    }},
                )

            results = []
            success_count = 0
            error_count = 0
            total_created = 0
            total_updated = 0
            total_api_hits = 0
            total_api_hit_cost_usd = 0.0

            for budget in budgets:
                latest_log = request.env['etp.project.aws.cost.fetch.log'].sudo().search(
                    [('budget_id', '=', budget.id)],
                    order='fetched_at desc, id desc', limit=1,
                )
                try:
                    fetch_result = budget._fetch_cost_one(source='api_update_all')
                    created = int(fetch_result.get('created', 0))
                    updated = int(fetch_result.get('updated', 0))
                    api_hit_count = int(fetch_result.get('api_hit_count', 0))
                    api_hit_cost_usd = round(
                        float(fetch_result.get('api_hit_cost_usd', 0.0)), 4,
                    )
                    success_count += 1
                    total_created += created
                    total_updated += updated
                    total_api_hits += api_hit_count
                    total_api_hit_cost_usd += api_hit_cost_usd
                    results.append({
                        "budget_id": budget.id,
                        "budget_name": budget.name or "",
                        "project_id": budget.project_id.id if budget.project_id else False,
                        "project_name": budget.project_id.name if budget.project_id else "",
                        "tag_key": budget.tag_key or "",
                        "tag_value": budget.tag_value or "",
                        "status": "success",
                        "created": created,
                        "updated": updated,
                        "api_hit_count": api_hit_count,
                        "api_hit_cost_usd": api_hit_cost_usd,
                        "budget_amount": 0.0,
                        "total_consumed": 0.0,
                        "remaining": 0.0,
                        "percent_consumed": 0.0,
                        "daily_burn_rate": 0.0,
                        "last_fetched_at": (
                            budget.last_fetched_at.strftime("%Y-%m-%d %H:%M:%S")
                            if budget.last_fetched_at else ""
                        ),
                    })
                except UserError as e:
                    error_count += 1
                    new_log = request.env['etp.project.aws.cost.fetch.log'].sudo().search(
                        [('budget_id', '=', budget.id)],
                        order='fetched_at desc, id desc', limit=1,
                    )
                    if new_log and new_log != latest_log:
                        api_hit_count = int(new_log.api_hit_count or 0)
                        api_hit_cost_usd = round(float(new_log.api_hit_cost_usd or 0.0), 4)
                    else:
                        api_hit_count = 0
                        api_hit_cost_usd = 0.0
                    total_api_hits += api_hit_count
                    total_api_hit_cost_usd += api_hit_cost_usd
                    results.append({
                        "budget_id": budget.id,
                        "budget_name": budget.name or "",
                        "project_id": budget.project_id.id if budget.project_id else False,
                        "project_name": budget.project_id.name if budget.project_id else "",
                        "tag_key": budget.tag_key or "",
                        "tag_value": budget.tag_value or "",
                        "status": "error",
                        "error": str(e),
                        "created": 0,
                        "updated": 0,
                        "api_hit_count": api_hit_count,
                        "api_hit_cost_usd": api_hit_cost_usd,
                        "budget_amount": 0.0,
                        "total_consumed": 0.0,
                        "remaining": 0.0,
                        "percent_consumed": 0.0,
                        "daily_burn_rate": 0.0,
                        "last_fetched_at": (
                            budget.last_fetched_at.strftime("%Y-%m-%d %H:%M:%S")
                            if budget.last_fetched_at else ""
                        ),
                    })
                except Exception as e:
                    error_count += 1
                    _logger.exception(
                        "AWS cost fetch failed for budget id=%s name=%s",
                        budget.id, budget.name,
                    )
                    new_log = request.env['etp.project.aws.cost.fetch.log'].sudo().search(
                        [('budget_id', '=', budget.id)],
                        order='fetched_at desc, id desc', limit=1,
                    )
                    if new_log and new_log != latest_log:
                        api_hit_count = int(new_log.api_hit_count or 0)
                        api_hit_cost_usd = round(float(new_log.api_hit_cost_usd or 0.0), 4)
                    else:
                        api_hit_count = 0
                        api_hit_cost_usd = 0.0
                    total_api_hits += api_hit_count
                    total_api_hit_cost_usd += api_hit_cost_usd
                    results.append({
                        "budget_id": budget.id,
                        "budget_name": budget.name or "",
                        "project_id": budget.project_id.id if budget.project_id else False,
                        "project_name": budget.project_id.name if budget.project_id else "",
                        "tag_key": budget.tag_key or "",
                        "tag_value": budget.tag_value or "",
                        "status": "error",
                        "error": str(e),
                        "created": 0,
                        "updated": 0,
                        "api_hit_count": api_hit_count,
                        "api_hit_cost_usd": api_hit_cost_usd,
                        "budget_amount": 0.0,
                        "total_consumed": 0.0,
                        "remaining": 0.0,
                        "percent_consumed": 0.0,
                        "daily_burn_rate": 0.0,
                        "last_fetched_at": fields.Datetime.to_string(budget.last_fetched_at) if budget.last_fetched_at else "",
                    })
 
            return return_Response(
                message="AWS cost update completed.",
                status=200,
                data={"data": {
                    "total_budgets": len(budgets),
                    "success_count": success_count,
                    "error_count": error_count,
                    "total_created": total_created,
                    "total_updated": total_updated,
                    "total_api_hits": total_api_hits,
                    "total_api_hit_cost_usd": round(total_api_hit_cost_usd, 4),
                    "results": results,
                }},
            )
        except Exception as e:
            _logger.exception("update_all_aws_cost failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/etp_projects/aws_cost/update_all',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def status_all_aws_cost(self, **params):
        try:
            jdata = self._read_json_body()

            include_inactive = bool(jdata.get('include_inactive'))
            domain = []
            if not include_inactive:
                domain.append(('active', '=', True))

            budget_ids = jdata.get('budget_ids') or []
            project_ids = jdata.get('project_ids') or []
            if budget_ids:
                if not isinstance(budget_ids, list) or not all(isinstance(x, int) for x in budget_ids):
                    return return_Response(
                        message="'budget_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('id', 'in', budget_ids))
            if project_ids:
                if not isinstance(project_ids, list) or not all(isinstance(x, int) for x in project_ids):
                    return return_Response(
                        message="'project_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('project_id', 'in', project_ids))

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(domain)

            if not budgets:
                return return_Response(
                    message="No AWS budgets matched the given filters.",
                    status=200,
                    data={"data": {
                        "total_budgets": 0,
                        "success_count": 0,
                        "error_count": 0,
                        "total_created": 0,
                        "total_updated": 0,
                        "results": [],
                    }},
                )

            results = []
            for budget in budgets:
                results.append({
                    "budget_id": budget.id,
                    "budget_name": budget.name or "",
                    "project_id": budget.project_id.id if budget.project_id else False,
                    "project_name": budget.project_id.name if budget.project_id else "",
                    "tag_key": budget.tag_summary or "",
                    "tag_value": budget.tag_summary or "",
                    "status": "success",
                    "created": 0,
                    "updated": 0,
                    "budget_amount": 0.0,
                    "total_consumed": 0.0,
                    "remaining": 0.0,
                    "percent_consumed": 0.0,
                    "daily_burn_rate": 0.0,
                    "last_fetched_at": (
                        budget.last_fetched_at.strftime("%Y-%m-%d %H:%M:%S")
                        if budget.last_fetched_at else ""
                    ),
                })

            return return_Response(
                message="AWS cost status retrieved.",
                status=200,
                data={"data": {
                    "total_budgets": len(budgets),
                    "success_count": len(budgets),
                    "error_count": 0,
                    "total_created": 0,
                    "total_updated": 0,
                    "results": results,
                }},
            )
        except Exception as e:
            _logger.exception("status_all_aws_cost failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/etp_projects/aws_budget/list',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_aws_budgets(self, **params):
        try:
            jdata = self._read_json_body()

            domain = []
            if not bool(jdata.get('include_inactive')):
                domain.append(('active', '=', True))

            budget_ids = jdata.get('budget_ids') or []
            project_ids = jdata.get('project_ids') or []
            if budget_ids:
                if not isinstance(budget_ids, list) or not all(isinstance(x, int) for x in budget_ids):
                    return return_Response(
                        message="'budget_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('id', 'in', budget_ids))
            if project_ids:
                if not isinstance(project_ids, list) or not all(isinstance(x, int) for x in project_ids):
                    return return_Response(
                        message="'project_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('project_id', 'in', project_ids))

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(domain, order='project_id, name')

            records = []
            for budget in budgets:
                model_lines = []
                for ml in budget.model_line_ids:
                    model_lines.append({
                        "id": ml.id,
                        "ai_model_id": ml.ai_model_id.id if ml.ai_model_id else False,
                        "ai_model_name": ml.ai_model_id.name if ml.ai_model_id else "",
                        "per_task_cost": ml.per_task_cost or 0.0,
                    })

                infra_lines = []
                for il in budget.infra_line_ids:
                    infra_lines.append({
                        "id": il.id,
                        "infra_type_id": il.infra_type_id.id if il.infra_type_id else False,
                        "infra_type_name": il.infra_type_id.name if il.infra_type_id else "",
                        "description": il.description or "",
                        "budget_amount": il.budget_amount or 0.0,
                    })

                records.append({
                    "id": budget.id,
                    "seq": budget.name or "",
                    "project_id": budget.project_id.id if budget.project_id else False,
                    "project_name": budget.project_id.name if budget.project_id else "",
                    "project_budget": 0.0,
                    "final_budget": 0.0,
                    "total_used_cost": 0.0,
                    "remaining_cost": 0.0,
                    "percent_consumed": 0.0,
                    "currency": "",
                    "currency_symbol": "",
                    "daily_burn_rate": 0.0,
                    "runway_days": 0,
                    "runway_days_exact": 0.0,
                    "runway_depletes_on": "",
                    "model_lines": model_lines,
                    "infra_lines": infra_lines,
                })

            return return_Response(
                message="OK",
                status=200,
                data={"data": {
                    "total": len(records),
                    "records": records,
                }},
            )
        except Exception as e:
            _logger.exception("list_aws_budgets failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    DEFAULT_BURN_GRAPH_DAYS = 30
    MAX_BURN_GRAPH_DAYS = 365

    @staticmethod
    def _pct(part, whole):
        if not whole:
            return 0.0
        return round((part / whole) * 100.0, 2)

    @staticmethod
    def _round2(value):
        return round(float(value or 0.0), 2)

    @staticmethod
    def _parse_date_param(raw, label):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date(), None
        except ValueError:
            return None, return_Response(
                message=f"Invalid {label} '{raw}'. Expected YYYY-MM-DD.",
                status=400,
            )

    @classmethod
    def _parse_positive_int_param(cls, raw, label, default, maximum):
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

    @staticmethod
    def _parse_positive_float_param(raw, label):
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

    @classmethod
    def _resolve_budget_info_filters(cls, params):
        raw_start = (params.get("start_date") or "").strip()
        raw_end = (params.get("end_date") or "").strip()
        start = end = None
        if raw_start:
            start, error = cls._parse_date_param(raw_start, "start_date")
            if error is not None:
                return None, error
        if raw_end:
            end, error = cls._parse_date_param(raw_end, "end_date")
            if error is not None:
                return None, error
        if start and end and start > end:
            return None, return_Response(
                message="Invalid date range: start_date must be on or before end_date.",
                status=400,
            )

        days, error = cls._parse_positive_int_param(
            (params.get("graph_days") or "").strip(),
            "graph_days",
            cls.DEFAULT_BURN_GRAPH_DAYS,
            cls.MAX_BURN_GRAPH_DAYS,
        )
        if error is not None:
            return None, error

        target_aht, error = cls._parse_positive_float_param(
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

    @staticmethod
    def _resolve_budget_info_project_id(env, params):
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

    @staticmethod
    def _budget_info_budget_domain(project_id, include_inactive):
        domain = []
        if not include_inactive:
            domain.append(("active", "=", True))
        if project_id:
            domain.append(("project_id", "=", project_id))
        return domain

    @staticmethod
    def _budget_info_cost_line_domain(budgets, filters):
        domain = [
            ("budget_id", "in", budgets.ids),
            ("is_model_breakdown", "=", False),
        ]
        if filters["start"]:
            domain.append(("period", ">=", filters["start"].replace(day=1)))
        if filters["end"]:
            domain.append(("period", "<=", filters["end"]))
        return domain

    @classmethod
    def _build_budget_info_kpi(cls, env, project_id, include_inactive):
        Budget = env["etp.project.aws.budget"].sudo()
        budgets = Budget.search(
            cls._budget_info_budget_domain(project_id, include_inactive)
        )

        total_budget = sum(b.budget_amount or 0.0 for b in budgets)
        total_consumed = sum(b.consumed_amount or 0.0 for b in budgets)
        total_remaining = total_budget - total_consumed

        today = date.today()
        burn_window_start = today - timedelta(days=6)
        Line = env["etp.project.aws.cost.line"].sudo()
        recent_lines = Line.search([
            ("budget_id", "in", budgets.ids),
            ("is_model_breakdown", "=", False),
            ("granularity", "=", "day"),
            ("period", ">=", burn_window_start),
            ("period", "<=", today),
        ]) if budgets else Line.browse()
        recent_total = sum(line.amount_source or 0.0 for line in recent_lines)
        daily_burn = recent_total / 7.0 if recent_total else 0.0

        runway_days = None
        if daily_burn > 0:
            runway_days = int(total_remaining / daily_burn) if total_remaining > 0 else 0

        return {
            "budget_count": len(budgets),
            "total_budget": {
                "amount": cls._round2(total_budget),
                "percentage": 100.0 if total_budget else 0.0,
            },
            "total_consumed": {
                "amount": cls._round2(total_consumed),
                "percentage": cls._pct(total_consumed, total_budget),
            },
            "total_remaining": {
                "amount": cls._round2(total_remaining),
                "percentage": cls._pct(total_remaining, total_budget),
            },
            "daily_burn_rate": {
                "amount": cls._round2(daily_burn),
                "percentage": cls._pct(daily_burn, total_budget),
            },
            "runway_days": runway_days,
        }, budgets

    @classmethod
    def _build_budget_info_service_costs(cls, env, budgets, filters):
        if not budgets:
            return {"total_amount": 0.0, "services": []}
        Line = env["etp.project.aws.cost.line"].sudo()
        lines = Line.search(cls._budget_info_cost_line_domain(budgets, filters))

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
                "amount": cls._round2(amount),
                "percentage": cls._pct(amount, grand_total),
            }
            for name, amount in sorted(totals.items(), key=lambda item: -item[1])
        ]
        return {
            "total_amount": cls._round2(grand_total),
            "services": breakdown,
        }

    @classmethod
    def _build_budget_info_aht_overview(cls, filters):
        target = filters["target_aht"]
        return {
            "aht_measured_count": 0,
            "aht_total_minutes": 0.0,
            "aht_average_minutes": 0.0,
            "target_aht_minutes": cls._round2(target) if target else None,
            "target_indicator": "no_target" if not target else "no_data",
        }

    @classmethod
    def _build_budget_info_daily_burn_graph(cls, env, budgets, filters):
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
        lines = Line.search([
            ("budget_id", "in", budgets.ids),
            ("is_model_breakdown", "=", False),
            ("granularity", "=", "day"),
            ("period", ">=", start),
            ("period", "<=", end),
        ])

        daily_by_date = {}
        for line in lines:
            day = line.period
            if not day or not (start <= day <= end):
                continue
            amount = float(line.amount_source or 0.0)
            if amount <= 0:
                continue
            daily_by_date[day] = daily_by_date.get(day, 0.0) + amount

        cursor = start
        while cursor <= end:
            amount = daily_by_date.get(cursor, 0.0)
            series.append({"date": cursor.isoformat(), "amount": cls._round2(amount)})
            cursor += timedelta(days=1)

        total = sum(point["amount"] for point in series)
        peak = max(series, key=lambda p: p["amount"]) if series else None
        if peak and peak["amount"] == 0.0:
            peak = None
        average = total / len(series) if series else 0.0
        return {
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "total_amount": cls._round2(total),
            "average_per_day": cls._round2(average),
            "peak_day": peak,
            "series": series,
        }

    @classmethod
    def _build_budget_info_burn_per_batch(cls, budgets):
        batches = budgets.mapped("batch_budget_ids") if budgets else None
        rows = []
        for batch in (batches or []):
            if batch.state in ("rejected", "withdrawn"):
                continue
            approved = batch.approved_amount or 0.0
            consumed = batch.consumed_cost or 0.0
            rate = round((consumed / approved) * 100) if approved else 0
            if rate >= 80:
                status = "approved"
            elif rate >= 50:
                status = "partial"
            else:
                status = "pending"
            rows.append({
                "batch_id": batch.name or "",
                "videos": batch.total_tasks or 0,
                "burn": cls._round2(consumed),
                "approval": {"rate": rate, "status": status},
                "feedback": "",
                "tasks": [],
            })
        return {
            "title": "Burn per batch",
            "batches": rows,
        }

    @staticmethod
    def _budget_info_max_last_fetched_at(budgets):
        if not budgets:
            return ""
        ts = max((b.last_fetched_at for b in budgets if b.last_fetched_at), default=None)
        return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else ""

    @staticmethod
    def _budget_info_latest_fetch_log(env, budgets):
        if not budgets:
            return None
        log = env["etp.project.aws.cost.fetch.log"].sudo().search(
            [("budget_id", "in", budgets.ids)],
            order="fetched_at desc, id desc",
            limit=1,
        )
        return log.to_api_dict() if log else None

    @classmethod
    def _build_budget_info_payload(cls, env, project_id, include_inactive, filters):
        kpi, budgets = cls._build_budget_info_kpi(env, project_id, include_inactive)
        service_costs = cls._build_budget_info_service_costs(env, budgets, filters)
        aht_overview = cls._build_budget_info_aht_overview(filters)
        burn_graph = cls._build_budget_info_daily_burn_graph(env, budgets, filters)

        return {
            "filters": {
                "project_id": project_id,
                "include_inactive": include_inactive,
                "start_date": filters["start"].isoformat() if filters["start"] else None,
                "end_date": filters["end"].isoformat() if filters["end"] else None,
                "graph_days": filters["graph_days"],
                "target_aht_minutes": (
                    cls._round2(filters["target_aht"]) if filters["target_aht"] else None
                ),
            },
            "kpi": kpi,
            "service_costs": service_costs,
            "aht_overview": aht_overview,
            "daily_burn_graph": burn_graph,
            "budget_timeline": {},
            "burn_per_batch": cls._build_budget_info_burn_per_batch(budgets),
            "allocation_ledger": {},
            "last_fetched_at": cls._budget_info_max_last_fetched_at(budgets),
            "last_fetch_log": cls._budget_info_latest_fetch_log(env, budgets),
        }

    @http.route(
        '/api/v1/etp_projects/budget/info',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def etp_project_budget_info(self, **kwargs):
        env = request.env
        params = request.params or {}

        project_id, error = self._resolve_budget_info_project_id(env, params)
        if error is not None:
            return error

        filters, error = self._resolve_budget_info_filters(params)
        if error is not None:
            return error

        include_inactive = (params.get("include_inactive") or "").strip().lower() in (
            "1", "true", "yes",
        )

        try:
            data = self._build_budget_info_payload(
                env, project_id, include_inactive, filters,
            )
        except Exception as e:
            _logger.exception("etp_project_budget_info failed")
            return return_Response(
                message="Failed to build budget info.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(message="OK", status=200, data=data)

    def _serialize_batch_budget(self, batch):
        batch.ensure_one()
        return {
            "id": batch.id,
            "name": batch.name or "",
            "project_budget_id": batch.project_budget_id.id,
            "project_id": batch.project_id.id if batch.project_id else False,
            "project_name": batch.project_id.name if batch.project_id else "",
            "connected_model": batch.connected_model or "",
            "total_tasks": batch.total_tasks or 0,
            "buffer_pct": batch.buffer_pct or 0.0,
            "estimated_cost": batch.estimated_cost or 0.0,
            "batch_budget": batch.batch_budget or 0.0,
            "approved_amount": batch.approved_amount or 0.0,
            "carried_over_amount": batch.carried_over_amount or 0.0,
            "start_date": str(batch.start_date) if batch.start_date else "",
            "end_date": str(batch.end_date) if batch.end_date else "",
            "state": batch.state or "draft",
            "model_lines": [
                {
                    "id": line.id,
                    "ai_model_id": line.ai_model_id.id,
                    "ai_model": line.ai_model_id.name or "",
                    "per_task_cost": line.per_task_cost or 0.0,
                }
                for line in batch.model_line_ids
            ],
        }

    @http.route(
        '/api/v1/etp_projects/batch_budget/create',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def create_batch_budget(self, **params):
        try:
            jdata = self._read_json_body()

            project_budget_id = jdata.get('project_budget_id')
            if not isinstance(project_budget_id, int):
                return return_Response(
                    message="'project_budget_id' (int) is required.",
                    status=400,
                )
            budget = request.env[BUDGET_MODEL].sudo().browse(project_budget_id)
            if not budget.exists():
                return return_Response(
                    message="Project budget %s not found." % project_budget_id,
                    status=404,
                )
            total_tasks = jdata.get('total_tasks')
            if not isinstance(total_tasks, int) or total_tasks <= 0:
                return return_Response(
                    message="'total_tasks' must be a positive integer.",
                    status=400,
                )

            start_date = (jdata.get('start_date') or '').strip()
            end_date = (jdata.get('end_date') or '').strip()
            if not start_date or not end_date:
                return return_Response(
                    message="'start_date' and 'end_date' (YYYY-MM-DD) are required.",
                    status=400,
                )
            if end_date < start_date:
                return return_Response(
                    message="'end_date' cannot be before 'start_date'.",
                    status=400,
                )

            buffer_pct = jdata.get('buffer_pct') or 0.0
            try:
                buffer_pct = float(buffer_pct)
            except (TypeError, ValueError):
                return return_Response(
                    message="'buffer_pct' must be a number.",
                    status=400,
                )
            if buffer_pct < 0:
                return return_Response(
                    message="'buffer_pct' cannot be negative.",
                    status=400,
                )

            vals = {
                'project_budget_id': project_budget_id,
                'total_tasks': total_tasks,
                'buffer_pct': buffer_pct,
                'start_date': start_date,
                'end_date': end_date,
            }

            model_lines = jdata.get('model_lines')
            if model_lines:
                if not isinstance(model_lines, list):
                    return return_Response(
                        message="'model_lines' must be a list.",
                        status=400,
                    )
                line_cmds = []
                for ln in model_lines:
                    if not isinstance(ln, dict):
                        return return_Response(
                            message="Each model line must be an object.",
                            status=400,
                        )
                    ai_model_id = ln.get('ai_model_id')
                    if not isinstance(ai_model_id, int):
                        return return_Response(
                            message="Each model line needs an int 'ai_model_id'.",
                            status=400,
                        )
                    per_task_cost = ln.get('per_task_cost') or 0.0
                    try:
                        per_task_cost = float(per_task_cost)
                    except (TypeError, ValueError):
                        return return_Response(
                            message="'per_task_cost' must be a number.",
                            status=400,
                        )
                    line_cmds.append((0, 0, {
                        'ai_model_id': ai_model_id,
                        'per_task_cost': per_task_cost,
                    }))
                vals['model_line_ids'] = line_cmds

            batch = request.env['etp.batch.budget'].sudo().create(vals)

            return return_Response(
                message="OK",
                status=200,
                data={"data": self._serialize_batch_budget(batch)},
            )
        except Exception as e:
            _logger.exception("create_batch_budget failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    def _parse_int_csv(self, raw):
        if raw is None or raw == '':
            return []
        parts = [p.strip() for p in str(raw).split(',') if p.strip()]
        out = []
        for p in parts:
            try:
                out.append(int(p))
            except ValueError:
                return None
        return out

    def _parse_str_csv(self, raw):
        if raw is None or raw == '':
            return []
        return [p.strip() for p in str(raw).split(',') if p.strip()]

    def _parse_bool(self, raw):
        if raw is None or raw == '':
            return False
        return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')

    @http.route(
        '/api/v1/etp_projects/batch_budget/list',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_batch_budgets(self, **params):
        try:
            domain = []
            if not self._parse_bool(params.get('include_inactive')):
                domain.append(('active', '=', True))

            batch_ids = self._parse_int_csv(params.get('batch_ids'))
            if batch_ids is None:
                return return_Response(
                    message="'batch_ids' must be a comma-separated list of integers.",
                    status=400,
                )
            if batch_ids:
                domain.append(('id', 'in', batch_ids))

            project_budget_ids = self._parse_int_csv(params.get('project_budget_ids'))
            if project_budget_ids is None:
                return return_Response(
                    message="'project_budget_ids' must be a comma-separated list of integers.",
                    status=400,
                )
            if project_budget_ids:
                domain.append(('project_budget_id', 'in', project_budget_ids))

            project_ids = self._parse_int_csv(params.get('project_ids'))
            if project_ids is None:
                return return_Response(
                    message="'project_ids' must be a comma-separated list of integers.",
                    status=400,
                )
            if project_ids:
                domain.append(('project_id', 'in', project_ids))

            states = self._parse_str_csv(params.get('states'))
            if states:
                domain.append(('state', 'in', states))

            try:
                limit = int(params.get('limit') or 50)
            except (TypeError, ValueError):
                return return_Response(
                    message="'limit' must be an integer.", status=400,
                )
            try:
                offset = int(params.get('offset') or 0)
            except (TypeError, ValueError):
                return return_Response(
                    message="'offset' must be an integer.", status=400,
                )
            limit = max(1, min(limit, 500))
            offset = max(0, offset)

            Batch = request.env['etp.batch.budget'].sudo()
            total = Batch.search_count(domain)
            batches = Batch.search(
                domain, limit=limit, offset=offset,
                order='create_date desc, id desc',
            )

            return return_Response(
                message="OK",
                status=200,
                data={"data": {
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "records": [self._serialize_batch_budget(b) for b in batches],
                }},
            )
        except Exception as e:
            _logger.exception("list_batch_budgets failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    def _serialize_topup(self, topup):
        topup.ensure_one()
        return {
            "id": topup.id,
            "name": topup.name or "",
            "project_budget_id": topup.project_budget_id.id,
            "project_budget_name": topup.project_budget_id.name or "",
            "project_id": topup.project_id.id if topup.project_id else False,
            "project_name": topup.project_id.name if topup.project_id else "",
            "amount": topup.amount or 0.0,
            "justification": topup.justification or "",
            "state": topup.state or "draft",
            "requester_id": topup.requester_id.id if topup.requester_id else False,
            "requester_name": topup.requester_id.name if topup.requester_id else "",
            "approver_id": topup.approver_id.id if topup.approver_id else False,
            "approver_name": topup.approver_id.name if topup.approver_id else "",
            "approval_date": (
                topup.approval_date.strftime("%Y-%m-%d %H:%M:%S")
                if topup.approval_date else ""
            ),
            "rejection_reason": topup.rejection_reason or "",
        }

    @http.route(
        '/api/v1/etp_projects/topup/create',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def create_topup(self, **params):
        try:
            jdata = self._read_json_body()

            project_budget_id = jdata.get('project_budget_id')
            if not isinstance(project_budget_id, int):
                return return_Response(
                    message="'project_budget_id' (int) is required.", status=400,
                )
            budget = request.env[BUDGET_MODEL].sudo().browse(project_budget_id)
            if not budget.exists():
                return return_Response(
                    message="Project budget %s not found." % project_budget_id,
                    status=404,
                )

            amount = jdata.get('amount')
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                return return_Response(
                    message="'amount' must be a positive number.", status=400,
                )
            if amount <= 0:
                return return_Response(
                    message="'amount' must be greater than zero.", status=400,
                )

            justification = (jdata.get('justification') or '').strip()
            if not justification:
                return return_Response(
                    message="'justification' is required.", status=400,
                )

            submit = jdata.get('submit')
            submit = True if submit is None else bool(submit)

            vals = {
                'project_budget_id': project_budget_id,
                'amount': amount,
                'justification': justification,
            }
            topup = request.env['etp.project.budget.topup'].sudo().create(vals)

            if submit:
                try:
                    topup.sudo().action_submit()
                except UserError as e:
                    return return_Response(
                        message=str(e), status=400, errors=[str(e)],
                    )

            return return_Response(
                message="OK",
                status=200,
                data={"data": self._serialize_topup(topup)},
            )
        except Exception as e:
            _logger.exception("create_topup failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        '/api/v1/etp_projects/topup/approve',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def approve_topup(self, **params):
        try:
            jdata = self._read_json_body()

            topup_id = jdata.get('topup_id')
            if not isinstance(topup_id, int):
                return return_Response(
                    message="'topup_id' (int) is required.", status=400,
                )

            topup = request.env['etp.project.budget.topup'].sudo().browse(topup_id)
            if not topup.exists():
                return return_Response(
                    message="Top-up %s not found." % topup_id, status=404,
                )

            try:
                topup.with_user(request.env.user).action_approve()
            except UserError as e:
                return return_Response(
                    message=str(e), status=400, errors=[str(e)],
                )

            return return_Response(
                message="OK",
                status=200,
                data={"data": self._serialize_topup(topup)},
            )
        except Exception as e:
            _logger.exception("approve_topup failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        '/api/v1/etp_projects/topup/reject',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def reject_topup(self, **params):
        try:
            jdata = self._read_json_body()

            topup_id = jdata.get('topup_id')
            if not isinstance(topup_id, int):
                return return_Response(
                    message="'topup_id' (int) is required.", status=400,
                )

            reason = (jdata.get('reason') or '').strip()
            if not reason:
                return return_Response(
                    message="'reason' is required.", status=400,
                )

            topup = request.env['etp.project.budget.topup'].sudo().browse(topup_id)
            if not topup.exists():
                return return_Response(
                    message="Top-up %s not found." % topup_id, status=404,
                )

            try:
                topup.with_user(request.env.user)._do_reject(reason)
            except UserError as e:
                return return_Response(
                    message=str(e), status=400, errors=[str(e)],
                )

            return return_Response(
                message="OK",
                status=200,
                data={"data": self._serialize_topup(topup)},
            )
        except Exception as e:
            _logger.exception("reject_topup failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        '/api/v1/etp_projects/aws_budget/export',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def export_aws_budgets(self, **params):
        try:
            try:
                import xlsxwriter
            except ImportError:
                return return_Response(
                    message="Python package 'xlsxwriter' is not installed on the server.",
                    status=400,
                )

            jdata = self._read_json_body()

            domain = []
            if not bool(jdata.get('include_inactive')):
                domain.append(('active', '=', True))

            budget_ids = jdata.get('budget_ids') or []
            project_ids = jdata.get('project_ids') or []
            if budget_ids:
                if not isinstance(budget_ids, list) or not all(isinstance(x, int) for x in budget_ids):
                    return return_Response(
                        message="'budget_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('id', 'in', budget_ids))
            if project_ids:
                if not isinstance(project_ids, list) or not all(isinstance(x, int) for x in project_ids):
                    return return_Response(
                        message="'project_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('project_id', 'in', project_ids))

            Budget = request.env[BUDGET_MODEL].sudo()
            budgets = Budget.search(domain, order='project_id, name')

            run_rate_end = date.today()
            run_rate_start = run_rate_end - timedelta(days=6)
            run_rate_days = 7.0

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            ws = wb.add_worksheet('Projects — Budget by Project')
            # Place outline +/- symbols on the parent row (above the group)
            ws.outline_settings(True, False, True, False)

            f_title = wb.add_format({
                'bold': True, 'font_size': 14, 'font_color': '#ffffff',
                'bg_color': '#1f2937', 'align': 'left', 'valign': 'vcenter',
            })
            f_kpi_label = wb.add_format({
                'bold': True, 'font_size': 10, 'font_color': '#374151',
                'bg_color': '#f3f4f6', 'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
            })
            f_kpi_value = wb.add_format({
                'font_size': 11, 'font_color': '#111827',
                'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
                'num_format': '#,##0.00',
            })
            f_kpi_value_text = wb.add_format({
                'font_size': 11, 'font_color': '#111827',
                'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
            })
            f_header = wb.add_format({
                'bold': True, 'font_size': 11, 'font_color': '#ffffff',
                'bg_color': '#374151', 'align': 'center', 'valign': 'vcenter',
                'border': 1, 'border_color': '#1f2937',
            })
            f_project = wb.add_format({
                'bold': True, 'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
            })
            f_text = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter',
            })
            f_text_model = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'left', 'valign': 'vcenter', 'indent': 2,
                'italic': True, 'font_color': '#4b5563',
            })
            f_blank = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter',
            })
            f_int = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '0',
            })
            f_money = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00',
            })
            f_money_actual = wb.add_format({
                'bold': True, 'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00',
            })
            f_money_child = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00',
                'font_color': '#4b5563', 'italic': True,
            })
            f_pct = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '0.00"%"',
            })
            f_pct_child = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '0.00"%"',
                'font_color': '#4b5563', 'italic': True,
            })
            f_runrate = wb.add_format({
                'font_size': 10, 'border': 1, 'border_color': '#e5e7eb',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00" /day"',
            })
            f_total_label = wb.add_format({
                'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                'border': 1, 'border_color': '#d1d5db',
                'align': 'left', 'valign': 'vcenter',
            })
            f_total_money = wb.add_format({
                'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                'border': 1, 'border_color': '#d1d5db',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00',
            })
            f_total_pct = wb.add_format({
                'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                'border': 1, 'border_color': '#d1d5db',
                'align': 'right', 'valign': 'vcenter', 'num_format': '0.00"%"',
            })
            f_total_runrate = wb.add_format({
                'bold': True, 'font_size': 10, 'bg_color': '#f9fafb',
                'border': 1, 'border_color': '#d1d5db',
                'align': 'right', 'valign': 'vcenter', 'num_format': '#,##0.00" /day"',
            })

            headers = [
                'Project', 'Estimated cost', 'Actual', 'Remaining',
                'Budget', 'Util %', 'Run rate', 'Top Model / Share',
            ]
            widths = [40, 16, 16, 16, 16, 10, 16, 22]
            for i, w in enumerate(widths):
                ws.set_column(i, i, w)

            ws.set_row(0, 28)
            ws.merge_range(0, 0, 0, len(headers) - 1, 'Projects — Budget by Project', f_title)

            project_set = {b.project_id.id for b in budgets if b.project_id}
            total_est = sum(b.total_approved_amount or 0.0 for b in budgets)
            total_actual = sum(b.consumed_amount or 0.0 for b in budgets)
            total_remaining = sum(b.remaining_amount or 0.0 for b in budgets)
            total_budget = sum(b.budget_amount or 0.0 for b in budgets)
            overall_pct = (total_actual / total_est * 100.0) if total_est else 0.0

            ws.write(2, 0, 'Total Budgets', f_kpi_label)
            ws.write_number(2, 1, len(budgets), f_kpi_value)
            ws.write(2, 2, 'Projects', f_kpi_label)
            ws.write_number(2, 3, len(project_set), f_kpi_value)
            ws.write(2, 4, 'Currency', f_kpi_label)
            ws.merge_range(2, 5, 2, 7, 'USD', f_kpi_value_text)

            ws.write(3, 0, 'Total Estimated', f_kpi_label)
            ws.write_number(3, 1, round(total_est, 2), f_kpi_value)
            ws.write(3, 2, 'Total Actual', f_kpi_label)
            ws.write_number(3, 3, round(total_actual, 2), f_kpi_value)
            ws.write(3, 4, 'Total Remaining', f_kpi_label)
            ws.merge_range(3, 5, 3, 7, round(total_remaining, 2), f_kpi_value)

            ws.write(4, 0, 'Overall Util %', f_kpi_label)
            ws.write_number(4, 1, round(overall_pct, 2), f_kpi_value)
            ws.write(4, 2, 'Run-rate Window', f_kpi_label)
            ws.merge_range(
                4, 3, 4, 7,
                '%s → %s (7-day daily avg)' % (
                    run_rate_start.strftime('%Y-%m-%d'),
                    run_rate_end.strftime('%Y-%m-%d'),
                ),
                f_kpi_value_text,
            )

            header_row = 6
            for col, h in enumerate(headers):
                ws.write(header_row, col, h, f_header)
            ws.set_row(header_row, 22)

            row_cursor = header_row + 1
            portfolio_run_rate_total = 0.0

            for b in budgets:
                project_name = b.project_id.name if b.project_id else "(no project)"
                budget_label = b.name or ""
                row_label = (
                    project_name
                    if budget_label in ("", project_name)
                    else "%s · %s" % (project_name, budget_label)
                )

                model_actual = {}
                model_quantity = {}
                run_rate_total = 0.0
                for line in b.cost_line_ids:
                    if line.granularity != "day":
                        continue
                    if line.is_model_breakdown:
                        model = (line.model_name or "").strip() or "(no model)"
                        amt = float(line.amount_source or 0.0)
                        qty = float(line.usage_quantity or 0.0)
                        if amt:
                            model_actual[model] = model_actual.get(model, 0.0) + amt
                        if qty:
                            model_quantity[model] = model_quantity.get(model, 0.0) + qty
                    else:
                        if line.period and run_rate_start <= line.period <= run_rate_end:
                            run_rate_total += float(line.amount_source or 0.0)

                portfolio_run_rate_total += run_rate_total
                run_rate_per_day = run_rate_total / run_rate_days

                top_model = ""
                if model_actual:
                    top_model = max(model_actual.items(), key=lambda kv: kv[1])[0]
                elif model_quantity:
                    # Cost-only providers (e.g. OpenAI) ship per-model rows with token
                    # quantities but no per-model USD; fall back to highest-volume model
                    # so the column reflects activity rather than being blank.
                    top_model = max(model_quantity.items(), key=lambda kv: kv[1])[0]

                ws.write(row_cursor, 0, row_label, f_project)
                ws.write_number(row_cursor, 1, round(b.total_approved_amount or 0.0, 2), f_money)
                ws.write_number(row_cursor, 2, round(b.consumed_amount or 0.0, 2), f_money_actual)
                ws.write_number(row_cursor, 3, round(b.remaining_amount or 0.0, 2), f_money)
                ws.write_number(row_cursor, 4, round(b.budget_amount or 0.0, 2), f_money)
                ws.write_number(row_cursor, 5, round(b.consumed_pct or 0.0, 2), f_pct)
                ws.write_number(row_cursor, 6, round(run_rate_per_day, 2), f_runrate)
                ws.write(row_cursor, 7, top_model or "—", f_text)
                row_cursor += 1

                project_actual = b.consumed_amount or 0.0
                model_keys = set(model_actual.keys()) | set(model_quantity.keys())
                child_models = sorted(
                    model_keys,
                    key=lambda m: (
                        -model_actual.get(m, 0.0),
                        -model_quantity.get(m, 0.0),
                        m,
                    ),
                )
                for model in child_models:
                    m_actual = model_actual.get(model, 0.0)
                    share_pct = (m_actual / project_actual * 100.0) if project_actual else 0.0
                    ws.write(row_cursor, 0, model, f_text_model)
                    ws.write(row_cursor, 1, '', f_blank)
                    ws.write_number(row_cursor, 2, round(m_actual, 2), f_money_child)
                    ws.write(row_cursor, 3, '', f_blank)
                    ws.write(row_cursor, 4, '', f_blank)
                    ws.write(row_cursor, 5, '', f_blank)
                    ws.write(row_cursor, 6, '', f_blank)
                    ws.write_number(row_cursor, 7, round(share_pct, 2), f_pct_child)
                    ws.set_row(row_cursor, None, None, {'level': 1, 'hidden': True})
                    row_cursor += 1

            if budgets:
                total_row = row_cursor
                ws.write(total_row, 0, 'Portfolio total', f_total_label)
                ws.write_number(total_row, 1, round(total_est, 2), f_total_money)
                ws.write_number(total_row, 2, round(total_actual, 2), f_total_money)
                ws.write_number(total_row, 3, round(total_remaining, 2), f_total_money)
                ws.write_number(total_row, 4, round(total_budget, 2), f_total_money)
                ws.write_number(total_row, 5, round(overall_pct, 2), f_total_pct)
                ws.write_number(
                    total_row, 6,
                    round(portfolio_run_rate_total / run_rate_days, 2),
                    f_total_runrate,
                )
                ws.write(total_row, 7, '—', f_total_label)

                ws.autofilter(header_row, 0, total_row - 1, len(headers) - 1)
                ws.freeze_panes(header_row + 1, 1)

            ws2 = wb.add_worksheet('Service Spend')
            s_headers = [
                '#', 'Project', 'Budget Seq', 'Service',
                'Total Cost (USD)', '% of Budget',
            ]
            s_widths = [5, 28, 22, 36, 18, 14]
            for i, w in enumerate(s_widths):
                ws2.set_column(i, i, w)

            ws2.set_row(0, 28)
            ws2.merge_range(0, 0, 0, len(s_headers) - 1, 'Service-Wise Spend', f_title)

            service_rows = []
            service_grand_usd = 0.0
            for b in budgets:
                project_name = b.project_id.name if b.project_id else ""
                budget_seq = b.name or ""
                envelope = b.total_approved_amount or 0.0
                per_service = {}
                for line in b.cost_line_ids:
                    if line.is_model_breakdown:
                        continue
                    svc = (line.service_name or "Unknown").strip() or "Unknown"
                    agg = per_service.setdefault(svc, {'usd': 0.0})
                    agg['usd'] += float(line.amount_source or 0.0)
                for svc, agg in per_service.items():
                    pct = (agg['usd'] / envelope * 100.0) if envelope else 0.0
                    service_rows.append({
                        'project': project_name,
                        'budget_seq': budget_seq,
                        'service': svc,
                        'usd': agg['usd'],
                        'pct': pct,
                    })
                    service_grand_usd += agg['usd']

            service_rows.sort(key=lambda r: (r['project'], r['budget_seq'], -r['usd']))

            top_service_label = ''
            top_service_spend = 0.0
            if service_rows:
                top = max(service_rows, key=lambda r: r['usd'])
                top_service_label = top['service']
                top_service_spend = top['usd']

            ws2.write(2, 0, 'Total Services', f_kpi_label)
            ws2.write_number(2, 1, len({(r['budget_seq'], r['service']) for r in service_rows}), f_kpi_value)
            ws2.write(2, 2, 'Top Service', f_kpi_label)
            ws2.write(2, 3, top_service_label or '—', f_kpi_value_text)
            ws2.write(2, 4, 'Top Spend (USD)', f_kpi_label)
            ws2.write_number(2, 5, round(top_service_spend, 2), f_kpi_value)

            s_header_row = 4
            for col, h in enumerate(s_headers):
                ws2.write(s_header_row, col, h, f_header)
            ws2.set_row(s_header_row, 22)

            s_data_start = s_header_row + 1
            for idx, r in enumerate(service_rows, 1):
                row = s_data_start + idx - 1
                ws2.write_number(row, 0, idx, f_int)
                ws2.write(row, 1, r['project'], f_text)
                ws2.write(row, 2, r['budget_seq'], f_text)
                ws2.write(row, 3, r['service'], f_text)
                ws2.write_number(row, 4, round(r['usd'], 6), f_money)
                ws2.write_number(row, 5, round(r['pct'], 2), f_pct)

            if service_rows:
                s_total_row = s_data_start + len(service_rows)
                ws2.merge_range(s_total_row, 0, s_total_row, 3, 'Grand Total', f_total_label)
                ws2.write_number(s_total_row, 4, round(service_grand_usd, 6), f_total_money)
                ws2.write(s_total_row, 5, '', f_total_label)
                ws2.autofilter(s_header_row, 0, s_data_start + len(service_rows) - 1, len(s_headers) - 1)
                ws2.freeze_panes(s_header_row + 1, 3)

            wb.close()
            xlsx_bytes = output.getvalue()
            output.close()

            timestamp = fields.Datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = "aws_budget_export_%s.xlsx" % timestamp
            file_size = len(xlsx_bytes)
            file_b64 = base64.b64encode(xlsx_bytes).decode('utf-8')

            try:
                download_url = generate_s3_link(
                    file_b64, prefix='reports', filename=filename,
                )
            except Exception as e:
                _logger.exception("S3 upload failed for aws_budget export")
                return return_Response(
                    message="Failed to upload export to S3.",
                    status=500,
                    errors=[str(e)],
                )

            if not download_url:
                return return_Response(
                    message="S3 upload returned an empty link.",
                    status=500,
                )

            return return_Response(
                message="AWS budget export generated.",
                status=200,
                data={"data": {
                    "download_url": download_url,
                    "filename": filename,
                    "size": file_size,
                    "total_budgets": len(budgets),
                }},
            )
        except Exception as e:
            _logger.exception("export_aws_budgets failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/etp_projects/aws_cost/history',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def aws_cost_fetch_history(self, **params):
        try:
            jdata = self._read_json_body()

            domain = []

            budget_ids = jdata.get('budget_ids') or []
            project_ids = jdata.get('project_ids') or []
            if budget_ids:
                if not isinstance(budget_ids, list) or not all(isinstance(x, int) for x in budget_ids):
                    return return_Response(
                        message="'budget_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('budget_id', 'in', budget_ids))
            if project_ids:
                if not isinstance(project_ids, list) or not all(isinstance(x, int) for x in project_ids):
                    return return_Response(
                        message="'project_ids' must be a list of integers.",
                        status=400,
                    )
                domain.append(('project_id', 'in', project_ids))

            start_date = jdata.get('start_date')
            end_date = jdata.get('end_date')
            if start_date:
                if not isinstance(start_date, str):
                    return return_Response(
                        message="'start_date' must be a 'YYYY-MM-DD' string.",
                        status=400,
                    )
                domain.append(('fetched_at', '>=', start_date + ' 00:00:00'))
            if end_date:
                if not isinstance(end_date, str):
                    return return_Response(
                        message="'end_date' must be a 'YYYY-MM-DD' string.",
                        status=400,
                    )
                domain.append(('fetched_at', '<=', end_date + ' 23:59:59'))

            status_filter = jdata.get('status')
            if status_filter:
                if status_filter not in ('success', 'error'):
                    return return_Response(
                        message="'status' must be 'success' or 'error'.",
                        status=400,
                    )
                domain.append(('status', '=', status_filter))

            sources = jdata.get('sources') or []
            if sources:
                if not isinstance(sources, list) or not all(isinstance(x, str) for x in sources):
                    return return_Response(
                        message="'sources' must be a list of strings.",
                        status=400,
                    )
                domain.append(('source', 'in', sources))

            try:
                limit = int(jdata.get('limit') or 50)
            except (TypeError, ValueError):
                return return_Response(
                    message="'limit' must be an integer.",
                    status=400,
                )
            try:
                offset = int(jdata.get('offset') or 0)
            except (TypeError, ValueError):
                return return_Response(
                    message="'offset' must be an integer.",
                    status=400,
                )
            limit = max(1, min(limit, 500))
            offset = max(0, offset)

            FetchLog = request.env['etp.project.aws.cost.fetch.log'].sudo()
            total = FetchLog.search_count(domain)
            logs = FetchLog.search(
                domain, limit=limit, offset=offset, order='fetched_at desc, id desc',
            )

            records = [log.to_api_dict() for log in logs]

            return return_Response(
                message="OK",
                status=200,
                data={"data": {
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "records": records,
                }},
            )
        except Exception as e:
            _logger.exception("aws_cost_fetch_history failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )
