import logging

from odoo import fields, http
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


def _budget_summary(b):
    """Compact shape for the list view."""
    return {
        "id": b.id,
        "name": b.name or "",
        "project_id": b.project_id.id if b.project_id else False,
        "project_name": b.project_id.display_name if b.project_id else "",
        "budget_type": b.project_type or "",
        "budget_type_label": dict(_budget_types()).get(b.project_type, ""),
        "status": b.state or "",
        "status_label": dict(b._fields["state"].selection).get(b.state, ""),
    }


def _m2o(rec):
    """Serialize a Many2one as {id, display_name} or False (web_read style)."""
    return {"id": rec.id, "display_name": rec.display_name} if rec else False


def _resolve_budget(params):
    """Resolve ?budget_id=<int> to a budget record.
    Returns (budget, None) on success or (None, error_response)."""
    try:
        budget_id = int(params.get("budget_id"))
    except (TypeError, ValueError):
        return None, return_Response(
            message="'budget_id' query parameter is required and must be an integer.",
            status=400,
        )
    budget = request.env[BUDGET_MODEL].sudo().browse(budget_id)
    if not budget.exists():
        return None, return_Response(
            message="Budget %s not found." % budget_id, status=404,
        )
    return budget, None


def _batch_summary(batch):
    """Compact shape for the batches list view."""
    return {
        "id": batch.id,
        "name": batch.name or "",
        "start_date": _date(batch.start_date),
        "end_date": _date(batch.end_date),
        "total_tasks": batch.total_tasks or 0,
        "batch_budget": batch.batch_budget or 0.0,
        "approved_amount": batch.approved_amount or 0.0,
        "consumed_cost": batch.consumed_cost or 0.0,
        "remaining_cost": batch.remaining_cost or 0.0,
        "health_status": batch.health_status or "",
        "state": batch.state or "",
    }


def _batch_detail(batch):
    """Full detail of a single batch budget — all fields + nested lines."""
    return {
        "id": batch.id,
        "name": batch.name or "",
        "project_budget_id": _m2o(batch.project_budget_id),
        "project_id": _m2o(batch.project_id),
        "connected_model": batch.connected_model or "",
        "state": batch.state or "",
        "state_label": dict(batch._fields["state"].selection).get(batch.state, ""),
        # Financials
        "total_tasks": batch.total_tasks or 0,
        "estimated_cost": batch.estimated_cost or 0.0,
        "buffer_pct": batch.buffer_pct or 0.0,
        "batch_budget": batch.batch_budget or 0.0,
        "approved_amount": batch.approved_amount or 0.0,
        "carried_over_amount": batch.carried_over_amount or 0.0,
        "consumed_cost": batch.consumed_cost or 0.0,
        "consumed_pct": batch.consumed_pct or 0.0,
        "remaining_cost": batch.remaining_cost or 0.0,
        "closed_remaining": batch.closed_remaining or 0.0,
        "health_status": batch.health_status or "",
        # Dates / people
        "start_date": _date(batch.start_date),
        "end_date": _date(batch.end_date),
        "requester_id": _m2o(batch.requester_id),
        "approver_id": _m2o(batch.approver_id),
        "approval_date": _dt(batch.approval_date),
        "delivered_date": _dt(batch.delivered_date),
        "rejection_reason": batch.rejection_reason or "",
        "request_count": batch.request_count or 0,
        "s3_link": batch.s3_link or "",
        "active": bool(batch.active),
        # Nested lines
        "model_line_ids": [
            {
                "id": line.id,
                "ai_model_id": _m2o(line.ai_model_id),
                "per_task_cost": line.per_task_cost or 0.0,
            }
            for line in batch.model_line_ids
        ],
        "infra_line_ids": [
            {
                "id": line.id,
                "infra_type_id": _m2o(line.infra_type_id),
                "description": line.description or "",
                "budget_amount": line.budget_amount or 0.0,
            }
            for line in batch.infra_line_ids
        ],
        # Requests on this batch (summary)
        "request_ids": [
            {
                "id": req.id,
                "name": req.name or "",
                "state": req.state or "",
                "approved_total": getattr(req, "approved_total", 0.0) or 0.0,
            }
            for req in batch.request_ids
        ],
    }


def _dt(value):
    return fields.Datetime.to_string(value) if value else False


def _date(value):
    return fields.Date.to_string(value) if value else False


def _budget_detail(b):
    """Full detail of a budget — everything except top-ups, the cost-line
    service breakdown, and the fetch history."""
    return {
        "id": b.id,
        "name": b.name or "",
        "display_name": b.display_name or "",
        "project_id": _m2o(b.project_id),
        "project_type": b.project_type or "",
        "budget_type_label": dict(_budget_types()).get(b.project_type, ""),
        "status": b.state or "",
        "status_label": dict(b._fields["state"].selection).get(b.state, ""),
        "budget_amount": b.budget_amount or 0.0,
        "buffer_pct": b.buffer_pct or 0.0,
        "active": bool(b.active),
        "is_rnd": bool(b.is_rnd),

        # Financial summary (computed)
        "total_approved_amount": b.total_approved_amount or 0.0,
        "topup_total_amount": b.topup_total_amount or 0.0,
        "batch_budget_remain": b.batch_budget_remain or 0.0,
        "allocated_amount": b.allocated_amount or 0.0,
        "consumed_amount": b.consumed_amount or 0.0,
        "llm_consumed_amount": b.llm_consumed_amount or 0.0,
        "remaining_amount": b.remaining_amount or 0.0,
        "allocatable_amount": b.allocatable_amount or 0.0,
        "consumed_pct": b.consumed_pct or 0.0,
        "health_status": b.health_status or "",
        "cost_line_count": b.cost_line_count or 0,
        "last_fetched_at": _dt(b.last_fetched_at),

        # People
        "approver_user_ids": [_m2o(u) for u in b.approver_user_ids],
        "cto_user_id": _m2o(b.cto_user_id),

        # AWS config
        "aws_access_key_id": b.aws_access_key_id or False,
        "aws_secret_access_key": b.aws_secret_access_key or False,
        "aws_region": b.aws_region or False,
        "fetch_months": b.fetch_months or 0,
        "tag_line_ids": [
            {
                "id": t.id,
                "sequence": t.sequence,
                "tag_key": t.tag_key or "",
                "tag_value": t.tag_value or "",
                "active": bool(t.active),
            }
            for t in b.tag_line_ids
        ],

        # OpenRouter
        "openrouter_enabled": bool(b.openrouter_enabled),
        "openrouter_api_key": b.openrouter_api_key or False,
        "last_openrouter_fetched_at": _dt(b.last_openrouter_fetched_at),

        # Moonshot
        "moonshot_enabled": bool(b.moonshot_enabled),
        "moonshot_api_key": b.moonshot_api_key or False,
        "last_moonshot_fetched_at": _dt(b.last_moonshot_fetched_at),
        "moonshot_last_used_usd": b.moonshot_last_used_usd or 0.0,
        "moonshot_last_used_at": _dt(b.moonshot_last_used_at),

        # OpenAI
        "openai_enabled": bool(b.openai_enabled),
        "openai_api_key": b.openai_api_key or False,
        "openai_project_id": b.openai_project_id or False,
        "last_openai_fetched_at": _dt(b.last_openai_fetched_at),

        # GCP
        "gcp_enabled": bool(b.gcp_enabled),
        "gcp_project_id": b.gcp_project_id or False,
        "gcp_bq_dataset": b.gcp_bq_dataset or False,
        "gcp_bq_table": b.gcp_bq_table or False,
        "last_gcp_fetched_at": _dt(b.last_gcp_fetched_at),
        "gcp_service_filter": b.gcp_service_filter or False,
        "gcp_label_key": b.gcp_label_key or False,
        "gcp_label_value": b.gcp_label_value or False,
        "gcp_service_account_json": b.gcp_service_account_json or False,

        # Model lines (step 3)
        "model_line_ids": [
            {
                "id": line.id,
                "ai_model_id": _m2o(line.ai_model_id),
                "per_task_cost": line.per_task_cost or 0.0,
            }
            for line in b.model_line_ids
        ],
        # Infrastructure lines (step 4)
        "infra_line_ids": [
            {
                "id": line.id,
                "infra_type_id": _m2o(line.infra_type_id),
                "description": line.description or "",
                "budget_amount": line.budget_amount or 0.0,
            }
            for line in b.infra_line_ids
        ],
        "total_per_task_cost": sum(b.model_line_ids.mapped("per_task_cost")),
        "total_infra_budget": sum(b.infra_line_ids.mapped("budget_amount")),
    }


def _missing_ids(model, ids):
    """Return any ids that don't exist in `model` (so we can reject with a
    clean 400 instead of letting a DB foreign-key violation surface as a
    confusing 422)."""
    if not ids:
        return []
    found = set(request.env[model].sudo().browse(ids).exists().ids)
    return [i for i in ids if i not in found]


def _pagination(params, default_limit=100, max_limit=500):
    """Parse ?limit= & ?offset= query params.
    Returns (limit, offset, None) or (None, None, error_response)."""
    try:
        limit = min(max(int(params.get("limit") or default_limit), 1), max_limit)
        offset = max(int(params.get("offset") or 0), 0)
    except (TypeError, ValueError):
        return None, None, return_Response(
            message="'limit'/'offset' must be integers.", status=400,
        )
    return limit, offset, None


def _budget_to_dict(budget):
    return {
        "id": budget.id,
        "name": budget.name or "",
        "project_id": budget.project_id.id if budget.project_id else False,
        "project_name": budget.project_id.display_name if budget.project_id else "",
        "budget_type": budget.project_type or "",
        "budget_amount": budget.budget_amount or 0.0,
        "buffer_pct": budget.buffer_pct or 0.0,
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

    # GET — project dropdown list.  optional ?search=  ?limit=  ?offset=
    @http.route(
        "/api/v1/etp_projects/project_budget/projects",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_projects(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = [("active", "=", True)]
        search = (params.get("search") or "").strip()
        if search:
            domain.append(("name", "ilike", search))
        Project = request.env[PROJECT_MODEL].sudo()
        total = Project.search_count(domain)
        projects = Project.search(domain, order="name", limit=limit, offset=offset)
        return return_Response(
            message="OK", status=200,
            data={"data": {
                "total": total, "limit": limit, "offset": offset,
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

    # GET — list view of budgets (compact). Optional query filters:
    #   ?search=<text>  ?status=<state>  ?budget_type=rnd|operations
    #   ?project_id=<int>  ?include_inactive=1  ?limit=  ?offset=
    @http.route(
        "/api/v1/etp_projects/project_budget/list",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_list(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = []
        if not params.get("include_inactive"):
            domain.append(("active", "=", True))
        budget_type = params.get("budget_type")
        if budget_type:
            if budget_type not in dict(_budget_types()):
                return return_Response(message="'budget_type' is invalid.", status=400)
            domain.append(("project_type", "=", budget_type))
        status = params.get("status")
        if status:
            valid_states = dict(
                request.env[BUDGET_MODEL]._fields["state"].selection
            )
            if status not in valid_states:
                return return_Response(
                    message="'status' must be one of %s." % list(valid_states),
                    status=400,
                )
            domain.append(("state", "=", status))
        project_id = params.get("project_id")
        if project_id:
            try:
                domain.append(("project_id", "=", int(project_id)))
            except (TypeError, ValueError):
                return return_Response(
                    message="'project_id' must be an integer.", status=400,
                )
        search = (params.get("search") or "").strip()
        if search:
            # match on the budget name or its project name
            domain += [
                "|",
                ("name", "ilike", search),
                ("project_id.name", "ilike", search),
            ]
        Budget = request.env[BUDGET_MODEL].sudo()
        total = Budget.search_count(domain)
        budgets = Budget.search(
            domain, order="project_id, project_type, name",
            limit=limit, offset=offset,
        )
        return return_Response(
            message="OK", status=200,
            data={"data": {
                "total": total, "limit": limit, "offset": offset,
                "records": [_budget_summary(b) for b in budgets],
            }},
        )

    # GET — detail view of a single budget.  ?budget_id=<int>
    @http.route(
        "/api/v1/etp_projects/project_budget/detail",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_detail(self, **params):
        budget, err = _resolve_budget(params)
        if err:
            return err
        return return_Response(
            message="OK", status=200, data={"data": _budget_detail(budget)},
        )

    # GET — batches for a budget.  ?budget_id=<int>  optional ?limit=  ?offset=
    @http.route(
        "/api/v1/etp_projects/project_budget/batches",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_batches(self, **params):
        budget, err = _resolve_budget(params)
        if err:
            return err
        limit, offset, perr = _pagination(params)
        if perr:
            return perr
        Batch = request.env["etp.batch.budget"].sudo()
        domain = [("project_budget_id", "=", budget.id)]
        total = Batch.search_count(domain)
        batches = Batch.search(
            domain, order="create_date desc, id desc", limit=limit, offset=offset,
        )
        return return_Response(
            message="OK", status=200,
            data={"data": {
                "total": total, "limit": limit, "offset": offset,
                "records": [_batch_summary(batch) for batch in batches],
            }},
        )

    # GET — detail view of a single batch budget.  ?batch_id=<int>
    @http.route(
        "/api/v1/etp_projects/project_budget/batch_detail",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_batch_detail(self, **params):
        try:
            batch_id = int(params.get("batch_id"))
        except (TypeError, ValueError):
            return return_Response(
                message="'batch_id' query parameter is required and must be an integer.",
                status=400,
            )
        batch = request.env["etp.batch.budget"].sudo().browse(batch_id)
        if not batch.exists():
            return return_Response(
                message="Batch %s not found." % batch_id, status=404,
            )
        return return_Response(
            message="OK", status=200, data={"data": _batch_detail(batch)},
        )

    # GET — top-ups for a budget.  ?budget_id=<int>  optional ?limit=  ?offset=
    @http.route(
        "/api/v1/etp_projects/project_budget/topups",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_topups(self, **params):
        budget, err = _resolve_budget(params)
        if err:
            return err
        limit, offset, perr = _pagination(params)
        if perr:
            return perr
        Topup = request.env["etp.project.budget.topup"].sudo()
        domain = [("project_budget_id", "=", budget.id)]
        total = Topup.search_count(domain)
        topups = Topup.search(
            domain, order="create_date desc, id desc", limit=limit, offset=offset,
        )
        records = [
            {
                "id": t.id,
                "name": t.name or "",
                "project_id": _m2o(t.project_id),
                "amount": t.amount or 0.0,
                "justification": t.justification or "",
                "state": t.state or "",
                "state_label": dict(t._fields["state"].selection).get(t.state, ""),
                "requester_id": _m2o(t.requester_id),
                "approver_id": _m2o(t.approver_id),
                "approval_date": _dt(t.approval_date),
                "rejection_reason": t.rejection_reason or "",
                "create_date": _dt(t.create_date),
            }
            for t in topups
        ]
        return return_Response(
            message="OK", status=200,
            data={"data": {
                "total": total, "limit": limit, "offset": offset,
                "records": records,
            }},
        )

    # GET — cost lines / service breakdown for a budget.  ?budget_id=<int>
    #   optional: ?is_model_breakdown=1  ?source=aws|openai|...  ?limit=&offset=
    @http.route(
        "/api/v1/etp_projects/project_budget/cost_lines",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_cost_lines(self, **params):
        budget, err = _resolve_budget(params)
        if err:
            return err
        domain = [("budget_id", "=", budget.id)]
        if params.get("is_model_breakdown") is not None and params.get("is_model_breakdown") != "":
            domain.append((
                "is_model_breakdown", "=",
                params.get("is_model_breakdown") in ("1", "true", "True", True),
            ))
        if params.get("source"):
            domain.append(("source", "=", params.get("source")))
        limit, offset, perr = _pagination(params)
        if perr:
            return perr
        Line = request.env["etp.project.aws.cost.line"].sudo()
        total = Line.search_count(domain)
        lines = Line.search(
            domain, limit=limit, offset=offset, order="period desc, amount_source desc",
        )
        records = [
            {
                "id": line.id,
                "period": _date(line.period),
                "granularity": line.granularity or "",
                "service_name": line.service_name or "",
                "model_name": line.model_name or False,
                "token_type": line.token_type or False,
                "usage_quantity": line.usage_quantity or 0.0,
                "usage_unit": line.usage_unit or False,
                "is_model_breakdown": bool(line.is_model_breakdown),
                "source": line.source or "",
                "source_tag_key": line.source_tag_key or False,
                "source_tag_value": line.source_tag_value or False,
                "amount_source": line.amount_source or 0.0,
            }
            for line in lines
        ]
        return return_Response(
            message="OK", status=200,
            data={"data": {
                "total": total, "limit": limit, "offset": offset,
                "records": records,
            }},
        )

    # GET — fetch history for a budget.  ?budget_id=<int>  optional ?limit=&offset=
    @http.route(
        "/api/v1/etp_projects/project_budget/fetch_history",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_fetch_history(self, **params):
        budget, err = _resolve_budget(params)
        if err:
            return err
        limit, offset, perr = _pagination(params)
        if perr:
            return perr
        Log = request.env["etp.project.aws.cost.fetch.log"].sudo()
        domain = [("budget_id", "=", budget.id)]
        total = Log.search_count(domain)
        logs = Log.search(
            domain, limit=limit, offset=offset, order="fetched_at desc, id desc",
        )
        return return_Response(
            message="OK", status=200,
            data={"data": {
                "total": total, "limit": limit, "offset": offset,
                "records": [log.to_api_dict() for log in logs],
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

        # Buffer percentage (optional)
        if "buffer_pct" in jdata:
            try:
                vals["buffer_pct"] = float(jdata.get("buffer_pct") or 0.0)
            except (TypeError, ValueError):
                return return_Response(
                    message="'buffer_pct' must be a number.", status=400,
                )

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
        if jdata.get("status"):
            valid_states = dict(budget._fields["state"].selection)
            if jdata["status"] not in valid_states:
                return return_Response(
                    message="'status' must be one of %s." % list(valid_states),
                    status=400,
                )
            vals["state"] = jdata["status"]
        if "budget_amount" in jdata:
            vals["budget_amount"] = jdata["budget_amount"] or 0.0
        if "buffer_pct" in jdata:
            try:
                vals["buffer_pct"] = float(jdata.get("buffer_pct") or 0.0)
            except (TypeError, ValueError):
                return return_Response(
                    message="'buffer_pct' must be a number.", status=400,
                )

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
