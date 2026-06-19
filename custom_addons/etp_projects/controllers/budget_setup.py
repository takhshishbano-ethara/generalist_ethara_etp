import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)


class EtpProjectBudgetOptionsController(http.Controller):

    @http.route(
        "/api/v1/etp_projects/budget/approvers",
        methods=["GET"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
    )
    @validate_token
    def list_approvers(self, **kwargs):
        try:
            users = request.env["res.users"].sudo().search(
                [("active", "=", True), ("share", "=", False)],
                order="name",
            )
            options = [
                {
                    "id": u.id,
                    "name": u.name or "",
                    "login": u.login or "",
                    "email": u.partner_id.email or "",
                }
                for u in users
            ]
            return return_Response(
                message="OK",
                status=200,
                data={"data": {"total": len(options), "options": options}},
            )
        except Exception as e:
            _logger.exception("list_approvers failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        "/api/v1/etp_projects/budget/models",
        methods=["GET"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
    )
    @validate_token
    def list_models(self, **kwargs):
        try:
            models = request.env["etp.ai.model"].sudo().search(
                [("active", "=", True)],
            )
            options = [{"id": m.id, "name": m.name or ""} for m in models]
            return return_Response(
                message="OK",
                status=200,
                data={"data": {"total": len(options), "options": options}},
            )
        except Exception as e:
            _logger.exception("list_models failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        "/api/v1/etp_projects/budget/infrastructure",
        methods=["GET"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
    )
    @validate_token
    def list_infrastructure(self, **kwargs):
        try:
            infras = request.env["etp.infra.type"].sudo().search(
                [("active", "=", True)],
            )
            options = [
                {"id": i.id, "name": i.name or "", "code": i.code or ""}
                for i in infras
            ]
            return return_Response(
                message="OK",
                status=200,
                data={"data": {"total": len(options), "options": options}},
            )
        except Exception as e:
            _logger.exception("list_infrastructure failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        "/api/v1/etp_projects/budget/projects",
        methods=["GET"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
    )
    @validate_token
    def list_projects(self, **kwargs):
        try:
            projects = request.env["project.project"].sudo().search(
                [("active", "=", True)],
                order="name",
            )
            options = []
            for p in projects:
                options.append({
                    "id": p.id,
                    "name": p.name or "",
                    "code": getattr(p, "project_seq", "") or "",
                    "classification": getattr(p, "project_classification", "") or "",
                })
            return return_Response(
                message="OK",
                status=200,
                data={"data": {"total": len(options), "options": options}},
            )
        except Exception as e:
            _logger.exception("list_projects failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        "/api/v1/etp_projects/budget/types",
        methods=["GET"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
    )
    @validate_token
    def list_budget_types(self, **kwargs):
        try:
            field = request.env["etp.project.aws.budget"]._fields.get("project_type")
            selection = field.selection if field else []
            options = [{"value": k, "label": l} for k, l in (selection or [])]
            return return_Response(
                message="OK",
                status=200,
                data={"data": {"total": len(options), "options": options}},
            )
        except Exception as e:
            _logger.exception("list_budget_types failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    @http.route(
        "/api/v1/etp_projects/budget/details",
        methods=["GET"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
    )
    @validate_token
    def get_budget_details(self, **kwargs):
        try:
            raw_id = kwargs.get("budget_id")
            if raw_id in (None, ""):
                return return_Response(
                    message="'budget_id' query parameter is required.",
                    status=400,
                )
            try:
                bid = int(raw_id)
            except (TypeError, ValueError):
                return return_Response(
                    message="'budget_id' must be an integer.", status=400,
                )

            Budget = request.env["etp.project.aws.budget"].sudo()
            budget = Budget.browse(bid)
            if not budget.exists():
                return return_Response(
                    message="Project budget %s not found." % bid, status=400,
                )

            type_field = Budget._fields.get("project_type")
            type_labels = dict(type_field.selection) if type_field else {}

            approvers = [
                {
                    "id": u.id,
                    "name": u.name or "",
                    "login": u.login or "",
                    "email": u.partner_id.email or "",
                }
                for u in budget.approver_user_ids
            ]

            model_lines = []
            total_per_task_cost = 0.0
            for line in budget.model_line_ids:
                cost = float(line.per_task_cost or 0.0)
                total_per_task_cost += cost
                model_lines.append({
                    "id": line.id,
                    "model_id": line.ai_model_id.id if line.ai_model_id else False,
                    "model_name": line.ai_model_id.name if line.ai_model_id else "",
                    "per_task_cost": cost,
                })

            infra_lines = []
            total_infra_budget = 0.0
            for line in budget.infra_line_ids:
                amt = float(line.budget_amount or 0.0)
                total_infra_budget += amt
                infra_lines.append({
                    "id": line.id,
                    "infra_type_id": line.infra_type_id.id if line.infra_type_id else False,
                    "infra_type_name": line.infra_type_id.name if line.infra_type_id else "",
                    "description": line.description or "",
                    "budget_amount": amt,
                })

            return return_Response(
                message="OK",
                status=200,
                data={"data": {
                    "id": budget.id,
                    "project_details": {
                        "budget_name": budget.name or "",
                        "project": {
                            "id": budget.project_id.id if budget.project_id else False,
                            "name": budget.project_id.name if budget.project_id else "",
                        },
                        "budget_type": {
                            "value": budget.project_type or "",
                            "label": type_labels.get(budget.project_type, "") if budget.project_type else "",
                        },
                        "budget_usd": float(budget.budget_amount or 0.0),
                    },
                    "approvers": {
                        "total": len(approvers),
                        "items": approvers,
                    },
                    "models": {
                        "total": len(model_lines),
                        "items": model_lines,
                        "total_per_task_cost": round(total_per_task_cost, 6),
                    },
                    "infrastructure": {
                        "total": len(infra_lines),
                        "items": infra_lines,
                        "total_infrastructure_budget": round(total_infra_budget, 6),
                    },
                }},
            )
        except Exception as e:
            _logger.exception("get_budget_details failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )
