import json
import logging

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

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
        '/api/v1/etp_projects/aws_cost/update_all',
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
                        "results": [],
                    }},
                )

            results = []
            success_count = 0
            error_count = 0
            total_created = 0
            total_updated = 0

            for budget in budgets:
                try:
                    created, updated = budget._fetch_cost_one()
                    budget._maybe_alert_thresholds()
                    success_count += 1
                    total_created += created
                    total_updated += updated
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
                        "budget_amount": float(budget.budget_amount or 0.0),
                        "total_consumed": float(budget.total_consumed or 0.0),
                        "remaining": float(budget.remaining or 0.0),
                        "percent_consumed": round(float(budget.percent_consumed or 0.0), 2),
                        "daily_burn_rate": float(budget.daily_burn_rate or 0.0),
                        "last_fetched_at": (
                            budget.last_fetched_at.strftime("%Y-%m-%d %H:%M:%S")
                            if budget.last_fetched_at else ""
                        ),
                    })
                except UserError as e:
                    error_count += 1
                    results.append({
                        "budget_id": budget.id,
                        "budget_name": budget.name or "",
                        "project_id": budget.project_id.id if budget.project_id else False,
                        "project_name": budget.project_id.name if budget.project_id else "",
                        "status": "error",
                        "error": str(e),
                    })
                except Exception as e:
                    error_count += 1
                    _logger.exception(
                        "AWS cost fetch failed for budget id=%s name=%s",
                        budget.id, budget.name,
                    )
                    results.append({
                        "budget_id": budget.id,
                        "budget_name": budget.name or "",
                        "project_id": budget.project_id.id if budget.project_id else False,
                        "project_name": budget.project_id.name if budget.project_id else "",
                        "status": "error",
                        "error": str(e),
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
