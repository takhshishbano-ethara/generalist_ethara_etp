import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .dashboard import _read_json_body

_logger = logging.getLogger(__name__)

BUDGET_MODEL = "etp.project.aws.budget"
PROJECT_MODEL = "project.project"


def _budget_types():
    """Budget Type options read straight off the model's selection field."""
    field = request.env[BUDGET_MODEL].sudo().fields_get(["project_type"])
    return field["project_type"]["selection"]


def _missing_ids(model, ids):
    """Return any ids that don't exist in `model` (so we can reject with a
    clean 400 instead of letting a DB foreign-key violation surface as a
    confusing 422)."""
    if not ids:
        return []
    found = set(request.env[model].sudo().browse(ids).exists().ids)
    return [i for i in ids if i not in found]


def _budget_to_dict(budget):
    return {
        "id": budget.id,
        "name": budget.name or "",
        "project_id": budget.project_id.id if budget.project_id else False,
        "project_name": budget.project_id.display_name if budget.project_id else "",
        "budget_type": budget.project_type or "",
        "budget_amount": budget.budget_amount or 0.0,
        "approver_ids": budget.approver_user_ids.ids,
        "model_lines": [
            {
                "ai_model_id": line.ai_model_id.id,
                "model_name": line.ai_model_id.display_name,
                "per_task_cost": line.per_task_cost or 0.0,
            }
            for line in budget.model_line_ids
        ],
        "infra_lines": [
            {
                "infra_type_id": line.infra_type_id.id,
                "infra_name": line.infra_type_id.display_name,
                "description": line.description or "",
                "budget_amount": line.budget_amount or 0.0,
            }
            for line in budget.infra_line_ids
        ],
    }


class EtpProjectBudgetController(http.Controller):

    # GET — project dropdown list
    @http.route(
        "/api/v1/etp_projects/project_budget/projects",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_projects(self, **params):
        projects = request.env[PROJECT_MODEL].sudo().search(
            [("active", "=", True)], order="name",
        )
        return return_Response(
            message="OK", status=200,
            data={"data": {
                "projects": [{"id": p.id, "name": p.display_name} for p in projects],
            }},
        )

    # GET — budget type dropdown list
    @http.route(
        "/api/v1/etp_projects/project_budget/budget_types",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_budget_types(self, **params):
        return return_Response(
            message="OK", status=200,
            data={"data": {
                "budget_types": [
                    {"value": key, "label": label} for key, label in _budget_types()
                ],
            }},
        )

    # POST — create a project budget (whole 5-step wizard in one call)
    @http.route(
        "/api/v1/etp_projects/project_budget/create",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_create(self, **params):
        jdata = _read_json_body()

        # Step 1 — Project Details
        name = (jdata.get("name") or "").strip()
        project_id = jdata.get("project_id")
        budget_type = jdata.get("budget_type")
        budget_amount = jdata.get("budget_amount") or 0.0

        if not name:
            return return_Response(message="'name' is required.", status=400)
        if not isinstance(project_id, int):
            return return_Response(message="'project_id' is required.", status=400)
        if not request.env[PROJECT_MODEL].sudo().browse(project_id).exists():
            return return_Response(
                message="Project %s not found." % project_id, status=400,
            )
        if budget_type not in dict(_budget_types()):
            return return_Response(message="'budget_type' is invalid.", status=400)

        # One budget per (project, budget type) — a project may have one R&D
        # budget AND one Operations budget, but not two of the same type.
        existing = request.env[BUDGET_MODEL].sudo().search(
            [("project_id", "=", project_id), ("project_type", "=", budget_type)],
            limit=1,
        )
        if existing:
            return return_Response(
                message="A %s budget already exists for this project."
                % dict(_budget_types()).get(budget_type, budget_type),
                status=400,
            )

        vals = {
            "name": name,
            "project_id": project_id,
            "project_type": budget_type,
            "budget_amount": budget_amount,
        }

        # Step 2 — Approvals
        approver_ids = jdata.get("approver_ids") or []
        if approver_ids:
            if not all(isinstance(x, int) for x in approver_ids):
                return return_Response(
                    message="'approver_ids' must be a list of user ids.", status=400,
                )
            missing = _missing_ids("res.users", approver_ids)
            if missing:
                return return_Response(
                    message="Approver user(s) not found: %s" % missing, status=400,
                )
            vals["approver_user_ids"] = [(6, 0, approver_ids)]

        # Step 3 — Models
        model_lines = jdata.get("model_lines") or []
        line_cmds = []
        for line in model_lines:
            ai_model_id = line.get("ai_model_id")
            if not isinstance(ai_model_id, int):
                return return_Response(
                    message="Each model line needs an integer 'ai_model_id'.",
                    status=400,
                )
            line_cmds.append((0, 0, {
                "ai_model_id": ai_model_id,
                "per_task_cost": line.get("per_task_cost") or 0.0,
            }))
        missing = _missing_ids(
            "etp.ai.model", [c[2]["ai_model_id"] for c in line_cmds],
        )
        if missing:
            return return_Response(
                message="Model(s) not found: %s" % missing, status=400,
            )
        if line_cmds:
            vals["model_line_ids"] = line_cmds

        # Step 4 — Infrastructure
        infra_lines = jdata.get("infra_lines") or []
        infra_cmds = []
        for line in infra_lines:
            infra_type_id = line.get("infra_type_id")
            if not isinstance(infra_type_id, int):
                return return_Response(
                    message="Each infra line needs an integer 'infra_type_id'.",
                    status=400,
                )
            infra_cmds.append((0, 0, {
                "infra_type_id": infra_type_id,
                "description": line.get("description") or "",
                "budget_amount": line.get("budget_amount") or 0.0,
            }))
        missing = _missing_ids(
            "etp.infra.type", [c[2]["infra_type_id"] for c in infra_cmds],
        )
        if missing:
            return return_Response(
                message="Infrastructure type(s) not found: %s" % missing, status=400,
            )
        if infra_cmds:
            vals["infra_line_ids"] = infra_cmds

        budget = request.env[BUDGET_MODEL].sudo().create(vals)
        return return_Response(
            message="Project budget created.", status=200,
            data={"data": _budget_to_dict(budget)},
        )

    # PATCH — update a project budget
    @http.route(
        "/api/v1/etp_projects/project_budget/update",
        methods=["PATCH"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_update(self, **params):
        jdata = _read_json_body()
        budget_id = jdata.get("id")
        if not isinstance(budget_id, int):
            return return_Response(message="'id' is required.", status=400)

        budget = request.env[BUDGET_MODEL].sudo().browse(budget_id)
        if not budget.exists():
            return return_Response(message="Budget not found.", status=404)

        vals = {}

        # Step 1 — Project Details
        if jdata.get("name"):
            vals["name"] = jdata["name"].strip()
        if isinstance(jdata.get("project_id"), int):
            if not request.env[PROJECT_MODEL].sudo().browse(jdata["project_id"]).exists():
                return return_Response(
                    message="Project %s not found." % jdata["project_id"],
                    status=400,
                )
            vals["project_id"] = jdata["project_id"]
        if jdata.get("budget_type"):
            if jdata["budget_type"] not in dict(_budget_types()):
                return return_Response(message="'budget_type' is invalid.", status=400)
            vals["project_type"] = jdata["budget_type"]
        if "budget_amount" in jdata:
            vals["budget_amount"] = jdata["budget_amount"] or 0.0

        # Step 2 — Approvals (send the key to replace the whole set)
        if "approver_ids" in jdata:
            approver_ids = jdata.get("approver_ids") or []
            if not all(isinstance(x, int) for x in approver_ids):
                return return_Response(
                    message="'approver_ids' must be a list of user ids.", status=400,
                )
            missing = _missing_ids("res.users", approver_ids)
            if missing:
                return return_Response(
                    message="Approver user(s) not found: %s" % missing, status=400,
                )
            vals["approver_user_ids"] = [(6, 0, approver_ids)]

        # Step 3 — Models (send the key to replace all model lines)
        if "model_lines" in jdata:
            line_cmds = [(5, 0, 0)]
            for line in jdata.get("model_lines") or []:
                ai_model_id = line.get("ai_model_id")
                if not isinstance(ai_model_id, int):
                    return return_Response(
                        message="Each model line needs an integer 'ai_model_id'.",
                        status=400,
                    )
                line_cmds.append((0, 0, {
                    "ai_model_id": ai_model_id,
                    "per_task_cost": line.get("per_task_cost") or 0.0,
                }))
            missing = _missing_ids(
                "etp.ai.model", [c[2]["ai_model_id"] for c in line_cmds[1:]],
            )
            if missing:
                return return_Response(
                    message="Model(s) not found: %s" % missing, status=400,
                )
            vals["model_line_ids"] = line_cmds

        # Step 4 — Infrastructure (send the key to replace all infra lines)
        if "infra_lines" in jdata:
            infra_cmds = [(5, 0, 0)]
            for line in jdata.get("infra_lines") or []:
                infra_type_id = line.get("infra_type_id")
                if not isinstance(infra_type_id, int):
                    return return_Response(
                        message="Each infra line needs an integer 'infra_type_id'.",
                        status=400,
                    )
                infra_cmds.append((0, 0, {
                    "infra_type_id": infra_type_id,
                    "description": line.get("description") or "",
                    "budget_amount": line.get("budget_amount") or 0.0,
                }))
            missing = _missing_ids(
                "etp.infra.type", [c[2]["infra_type_id"] for c in infra_cmds[1:]],
            )
            if missing:
                return return_Response(
                    message="Infrastructure type(s) not found: %s" % missing,
                    status=400,
                )
            vals["infra_line_ids"] = infra_cmds

        budget.write(vals)
        return return_Response(
            message="Project budget updated.", status=200,
            data={"data": _budget_to_dict(budget)},
        )
