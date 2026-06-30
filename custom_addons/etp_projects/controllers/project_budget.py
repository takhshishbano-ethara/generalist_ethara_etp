import base64
import json
import logging

from odoo import fields, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    generate_s3_link,
    return_Response,
    validate_token,
)

from .dashboard import _coerce_date, _read_json_body

_logger = logging.getLogger(__name__)

BUDGET_MODEL = "etp.project.aws.budget"
PROJECT_MODEL = "project.project"
REQUEST_MODEL = "etp.batch.budget.request"
COST_LINE_MODEL = "etp.project.aws.cost.line"

LLM_SOURCES = ("openai", "openrouter", "moonshot", "gcp")
APPROVED_REQUEST_STATES = ("approved", "partially_approved")
ACTIVE_BATCH_STATES = ("approved", "in_progress", "draft")
COMPLETED_BATCH_STATES = ("delivered", "closed")


ATTACHMENT_PREFIX = "etp_projects/project_budget"


def _parse_attachment_links(raw):
    if not raw:
        return []
    return [s.strip() for s in str(raw).split(",") if s.strip()]


def _serialize_attachment_links(urls):
    seen = []
    for u in urls or []:
        if not u:
            continue
        s = str(u).strip()
        if s and s not in seen:
            seen.append(s)
    return ",".join(seen)


def _read_multipart_or_json():
    content_type = (request.httprequest.content_type or "").lower()
    if content_type.startswith("multipart/form-data"):
        files = list(request.httprequest.files.getlist("attachments") or [])
        raw = request.httprequest.form.get("payload") or ""
        jdata = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    jdata = parsed
            except (ValueError, TypeError):
                jdata = {}
        return jdata, files
    return (_read_json_body() or {}), []


def _upload_attachments_to_s3(files, prefix=ATTACHMENT_PREFIX):
    urls = []
    for f in files or []:
        try:
            content = f.read()
        except Exception:
            _logger.exception("project_budget attachment: failed to read uploaded file")
            continue
        if not content:
            continue
        try:
            b64 = base64.b64encode(content).decode("utf-8")
        except Exception:
            _logger.exception("project_budget attachment: failed to base64-encode file")
            continue
        try:
            url = generate_s3_link(
                b64, prefix=prefix, filename=getattr(f, "filename", None),
            )
        except Exception:
            _logger.exception(
                "project_budget attachment: generate_s3_link raised for %s",
                getattr(f, "filename", "<unknown>"),
            )
            continue
        if url:
            urls.append(url)
    return urls


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


def _batch_llm_actual(batch, cache=None):
    """Sum of LLM-source cost lines in the batch date range; optional per-batch cache."""
    if not (batch and batch.project_id and batch.start_date and batch.end_date):
        return 0.0
    if cache is not None and batch.id in cache:
        return cache[batch.id]
    lines = request.env[COST_LINE_MODEL].sudo().search([
        ("project_id", "=", batch.project_id.id),
        ("granularity", "=", "day"),
        ("period", ">=", batch.start_date),
        ("period", "<=", batch.end_date),
        ("source", "in", list(LLM_SOURCES)),
    ])
    total = sum(lines.mapped("amount_source"))
    if cache is not None:
        cache[batch.id] = total
    return total


def _request_metrics(req, llm_cost_cache=None):
    """Estimation / actual LLM cost / buffer / variance / health for a request.

    estimation:         approved_total when approved/partially_approved, else requested_total.
    actual_llm_costing: LLM-source cost lines (openai/openrouter/moonshot/gcp) summed across
                        the batch's date range.
    buffer:             absolute USD buffer = subtotal x (buffer_pct / 100).
    variance:           estimation - actual_llm_costing (positive = under budget).
    health:             batch.health_status, or 'unknown' when no batch.
    """
    is_approved = req.state in APPROVED_REQUEST_STATES
    if is_approved:
        subtotal = (
            sum(req.model_line_ids.mapped("approved_amount"))
            + sum(req.infra_line_ids.mapped("approved_amount"))
        )
        estimation = req.approved_total or 0.0
    else:
        subtotal = (
            sum(req.model_line_ids.mapped("requested_amount"))
            + sum(req.infra_line_ids.mapped("requested_amount"))
        )
        estimation = req.requested_total or 0.0

    buffer_amount = subtotal * ((req.buffer_pct or 0.0) / 100.0)
    actual_llm = _batch_llm_actual(req.batch_id, cache=llm_cost_cache)
    batch = req.batch_id
    health = (batch.health_status or "unknown") if batch else "unknown"

    return {
        "estimation": estimation,
        "actual_llm_costing": actual_llm,
        "buffer": buffer_amount,
        "variance": estimation - actual_llm,
        "health": health,
    }


def _request_to_dict(req, llm_cost_cache=None):
    """Serialize a batch budget request."""
    return {
        "id": req.id,
        "name": req.name or "",
        "sequence_number": req.sequence_number or 0,
        "state": req.state or "",
        "state_label": dict(req._fields["state"].selection).get(req.state, ""),
        "batch_id": _m2o(req.batch_id),
        "project_budget_id": _m2o(req.project_budget_id),
        "project_id": _m2o(req.project_id),
        "requester_id": _m2o(req.requester_id),
        "approver_id": _m2o(req.approver_id),
        "request_date": _dt(req.request_date),
        "approval_date": _dt(req.approval_date),
        "priority": req.priority or "",
        "justification": req.justification or "",
        "subject": req.subject or "",
        "message": req.message or "",
        "total_tasks": req.total_tasks or 0,
        "buffer_pct": req.buffer_pct or 0.0,
        "requested_total": req.requested_total or 0.0,
        "approved_total": req.approved_total or 0.0,
        "rejection_reason": req.rejection_reason or "",
        "attachments": [
            {
                "id": a.id,
                "name": a.name or "",
                "mimetype": a.mimetype or "",
            }
            for a in req.attachment_ids
        ],
        "model_lines": [
            {
                "id": line.id,
                "ai_model_id": _m2o(line.ai_model_id),
                "description": line.description or "",
                "per_task_cost": line.per_task_cost or 0.0,
                "requested_amount": line.requested_amount or 0.0,
                "approved_amount": line.approved_amount or 0.0,
            }
            for line in req.model_line_ids
        ],
        "infra_lines": [
            {
                "id": line.id,
                "infra_type_id": _m2o(line.infra_type_id),
                "description": line.description or "",
                "requested_amount": line.requested_amount or 0.0,
                "approved_amount": line.approved_amount or 0.0,
                "per_day_requested": line.per_day_requested or 0.0,
                "per_day_approved": line.per_day_approved or 0.0,
            }
            for line in req.infra_line_ids
        ],
        **_request_metrics(req, llm_cost_cache=llm_cost_cache),
    }


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
                "per_day_cost": line.per_day_cost or 0.0,
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


def _batch_phase_record(batch, llm_cost_cache=None):
    """Phase row — estimation falls back to batch_budget when no approved requests yet."""
    estimation = batch.approved_amount or batch.batch_budget or 0.0
    actual_llm = _batch_llm_actual(batch, cache=llm_cost_cache)
    health = batch.health_status or "unknown"
    return {
        "id": batch.id,
        "name": batch.name or "",
        "project_id": _m2o(batch.project_id),
        "project_budget_id": _m2o(batch.project_budget_id),
        "state": batch.state or "",
        "state_label": dict(batch._fields["state"].selection).get(batch.state, ""),
        "start_date": _date(batch.start_date),
        "end_date": _date(batch.end_date),
        "total_tasks": batch.total_tasks or 0,
        "estimation": estimation,
        "actual_llm_costing": actual_llm,
        "variance": estimation - actual_llm,
        "health_status": health,
        "health_status_label": dict(
            batch._fields["health_status"].selection
        ).get(health, ""),
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
                "per_day_cost": line.per_day_cost or 0.0,
            }
            for line in b.infra_line_ids
        ],
        "total_per_task_cost": sum(b.model_line_ids.mapped("per_task_cost")),
        "total_infra_budget": sum(b.infra_line_ids.mapped("budget_amount")),
        "attachment_ids": _parse_attachment_links(b.attachment_ids),
    }


def _missing_ids(model, ids):
    """Return any ids that don't exist in `model` (so we can reject with a
    clean 400 instead of letting a DB foreign-key violation surface as a
    confusing 422)."""
    if not ids:
        return []
    found = set(request.env[model].sudo().browse(ids).exists().ids)
    return [i for i in ids if i not in found]


VALID_COST_TYPES = ("per_task", "per_trajectory")


def _parse_model_line(line):
    if not isinstance(line, dict):
        return None, "Each model line must be an object."
    ai_model_id = line.get("ai_model_id")
    if not isinstance(ai_model_id, int):
        return None, "Each model line needs an integer 'ai_model_id'."
    cost_type = line.get("cost_type") or "per_task"
    if cost_type not in VALID_COST_TYPES:
        return None, "'cost_type' must be one of %s." % list(VALID_COST_TYPES)
    try:
        per_task_cost = float(line.get("per_task_cost") or 0.0)
        per_trajectory_cost = float(line.get("per_trajectory_cost") or 0.0)
    except (TypeError, ValueError):
        return None, "'per_task_cost'/'per_trajectory_cost' must be numbers."
    iterations = line.get("iterations") or 0
    if not isinstance(iterations, int) or iterations < 0:
        return None, "'iterations' must be a non-negative integer."
    if cost_type == "per_trajectory" and not per_task_cost:
        per_task_cost = per_trajectory_cost * iterations
    return {
        "ai_model_id": ai_model_id,
        "cost_type": cost_type,
        "per_task_cost": per_task_cost,
        "per_trajectory_cost": per_trajectory_cost,
        "iterations": iterations,
    }, None


def _parse_subscription_line(line):
    if not isinstance(line, dict):
        return None, None, "Each subscription line must be an object."
    sub_id = line.get("subscription_id")
    if not isinstance(sub_id, int):
        return None, None, "Each subscription line needs an integer 'subscription_id'."
    assigned_to = (
        line.get("assigned_to")
        or line.get("assigned_user_ids")
        or []
    )
    if not isinstance(assigned_to, list) or not all(
        isinstance(x, int) for x in assigned_to
    ):
        return None, None, "'assigned_to' must be a list of user ids."
    cost = line.get("cost")
    if cost is not None:
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            return None, None, "'cost' must be a number."
        if cost < 0:
            return None, None, "'cost' must be non-negative."
    return {
        "subscription_id": sub_id,
        "assigned_user_ids": [(6, 0, assigned_to)],
    }, cost, None


VALID_PRIORITIES = ("low", "normal", "high", "urgent")


def _normalize_batch_input(raw, fallback_buffer_pct=0.0):
    if not isinstance(raw, dict):
        return None, "Each batch entry must be an object."
    raw_tc = raw.get("task_count")
    task_count = raw_tc if isinstance(raw_tc, int) and raw_tc > 0 else 1
    today = fields.Date.context_today(request.env.user)
    try:
        start_date = _coerce_date(raw.get("start_date"), "start_date") or today
    except ValidationError:
        start_date = today
    try:
        end_date = _coerce_date(raw.get("end_date"), "end_date") or today
    except ValidationError:
        end_date = today
    if end_date < start_date:
        end_date = start_date
    raw_priority = raw.get("priority")
    priority = raw_priority if raw_priority in VALID_PRIORITIES else "normal"
    try:
        buffer_pct = float(raw.get("buffer_pct", fallback_buffer_pct) or 0.0)
    except (TypeError, ValueError):
        return None, "'buffer_pct' must be a number."
    return {
        "name": (raw.get("name") or "").strip() or None,
        "task_count": task_count,
        "start_date": start_date,
        "end_date": end_date,
        "justification": (raw.get("justification") or "").strip()
        or "Auto-generated batch budget request.",
        "priority": priority,
        "buffer_pct": buffer_pct,
    }, None


def _create_batch_with_request(budget, spec, budget_amount=0.0):
    batch_vals = {
        "project_budget_id": budget.id,
        "total_tasks": spec["task_count"],
        "start_date": spec["start_date"],
        "end_date": spec["end_date"],
        "buffer_pct": spec["buffer_pct"],
    }
    if spec.get("name"):
        batch_vals["name"] = spec["name"]
    if budget.infra_line_ids:
        batch_vals["infra_line_ids"] = [
            (0, 0, {
                "infra_type_id": line.infra_type_id.id,
                "description": line.description or False,
                "budget_amount": line.budget_amount or 0.0,
            })
            for line in budget.infra_line_ids
        ]
    batch = request.env["etp.batch.budget"].sudo().create(batch_vals)
    req = None
    if not (budget.model_line_ids or budget.infra_line_ids):
        return batch, req
    task_count = spec["task_count"]
    buffer_pct = spec["buffer_pct"]
    wiz_model_cmds = [
        (0, 0, {
            "ai_model_id": line.ai_model_id.id,
            "cost_type": line.cost_type or "per_task",
            "per_task_cost": line.per_task_cost or 0.0,
            "per_trajectory_cost": line.per_trajectory_cost or 0.0,
            "iterations": line.iterations or 0,
            "requested_amount": (task_count or 0) * (line.per_task_cost or 0.0),
        })
        for line in budget.model_line_ids
    ]
    wiz_infra_cmds = [
        (0, 0, {
            "infra_type_id": line.infra_type_id.id,
            "description": line.description or False,
            "requested_amount": line.budget_amount or 0.0,
        })
        for line in budget.infra_line_ids
    ]
    subtotal = (
        sum(c[2]["requested_amount"] for c in wiz_model_cmds)
        + sum(c[2]["requested_amount"] for c in wiz_infra_cmds)
    )
    suggested_total = subtotal * (1.0 + (buffer_pct / 100.0))
    if budget_amount and budget_amount > 0:
        suggested_total = budget_amount
    try:
        with request.env.cr.savepoint():
            wiz_vals = {
                "batch_id": batch.id,
                "justification": spec["justification"],
                "priority": spec["priority"],
                "total_tasks": task_count,
                "buffer_pct": buffer_pct,
                "requested_total": suggested_total,
            }
            if wiz_model_cmds:
                wiz_vals["model_line_ids"] = wiz_model_cmds
            if wiz_infra_cmds:
                wiz_vals["infra_line_ids"] = wiz_infra_cmds
            wizard = request.env["etp.batch.budget.request.wizard"].sudo().create(wiz_vals)
            result = wizard.action_submit()
            new_req_id = result.get("res_id") if isinstance(result, dict) else None
            if new_req_id:
                candidate = request.env[REQUEST_MODEL].sudo().browse(new_req_id)
                if candidate.exists():
                    req = candidate
    except Exception as wexc:
        _logger.warning(
            "batch budget request skipped for budget %s batch %s: %s",
            budget.id, batch.id, wexc,
        )
        req = None
    return batch, req


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
                "cost_type": line.cost_type or "per_task",
                "per_task_cost": line.per_task_cost or 0.0,
                "per_trajectory_cost": line.per_trajectory_cost or 0.0,
                "iterations": line.iterations or 0,
            }
            for line in budget.model_line_ids
        ],
        "infra_lines": [
            {
                "infra_type_id": line.infra_type_id.id,
                "infra_name": line.infra_type_id.display_name,
                "description": line.description or "",
                "budget_amount": line.budget_amount or 0.0,
                "per_day_cost": line.per_day_cost or 0.0,
            }
            for line in budget.infra_line_ids
        ],
        "subscription_lines": [
            {
                "subscription_id": line.subscription_id.id,
                "subscription_name": line.subscription_id.display_name,
                "cost": line.cost_per_subscription or 0.0,
                "assigned_to": line.assigned_user_ids.ids,
                "subscription_count": line.subscription_count or 0,
                "monthly_cost": line.final_amount or 0.0,
                "per_day_cost": line.per_day_cost or 0.0,
            }
            for line in budget.subscription_line_ids
        ],
        "attachment_ids": _parse_attachment_links(budget.attachment_ids),
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

    # GET — subscription catalog dropdown. Optional ?search=  ?include_inactive=1  ?limit=  ?offset=
    @http.route(
        "/api/v1/etp_projects/project_budget/subscriptions",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_subscriptions(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = []
        if not params.get("include_inactive"):
            domain.append(("active", "=", True))
        search = (params.get("search") or "").strip()
        if search:
            domain.append(("name", "ilike", search))
        Subscription = request.env["etp.subscription"].sudo()
        total = Subscription.search_count(domain)
        records = Subscription.search(
            domain, order="sequence, name", limit=limit, offset=offset,
        )
        return return_Response(
            message="OK", status=200,
            data={"data": {
                "total": total, "limit": limit, "offset": offset,
                "subscriptions": [
                    {
                        "id": s.id,
                        "name": s.name or "",
                        "cost": s.cost or 0.0,
                        "per_day_cost": (s.cost or 0.0) / 30.0,
                        "active": bool(s.active),
                    }
                    for s in records
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
                message="Phase %s not found." % batch_id, status=404,
            )
        return return_Response(
            message="OK", status=200, data={"data": _batch_detail(batch)},
        )

    @http.route(
        "/api/v1/etp_projects/project_budget/phases",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_phases(self, **params):
        Batch = request.env["etp.batch.budget"].sudo()
        base_domain = []
        if params.get("budget_id"):
            try:
                base_domain.append(
                    ("project_budget_id", "=", int(params["budget_id"])),
                )
            except (TypeError, ValueError):
                return return_Response(
                    message="'budget_id' must be an integer.", status=400,
                )
        if params.get("project_id"):
            try:
                base_domain.append(("project_id", "=", int(params["project_id"])))
            except (TypeError, ValueError):
                return return_Response(
                    message="'project_id' must be an integer.", status=400,
                )

        def _bucket(states, limit_key, offset_key):
            limit, offset, perr = _pagination({
                "limit": params.get(limit_key),
                "offset": params.get(offset_key),
            })
            if perr:
                return None, perr
            domain = base_domain + [("state", "in", list(states))]
            total = Batch.search_count(domain)
            batches = Batch.search(
                domain,
                order="end_date desc, create_date desc, id desc",
                limit=limit, offset=offset,
            )
            cache = {}
            return {
                "total": total, "limit": limit, "offset": offset,
                "records": [
                    _batch_phase_record(b, llm_cost_cache=cache) for b in batches
                ],
            }, None

        active, err = _bucket(
            ACTIVE_BATCH_STATES, "active_limit", "active_offset",
        )
        if err:
            return err
        completed, err = _bucket(
            COMPLETED_BATCH_STATES, "completed_limit", "completed_offset",
        )
        if err:
            return err
        return return_Response(
            message="OK", status=200,
            data={"data": {"active": active, "completed": completed}},
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

    # GET — list of batch budget requests.  Optional filters:
    #   ?state=<state>  ?batch_id=<int>  ?budget_id=<int>(project budget)
    #   ?project_id=<int>  ?search=<text>  ?limit=  ?offset=
    @http.route(
        "/api/v1/etp_projects/budget_request/list",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def budget_request_list(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        Request = request.env[REQUEST_MODEL].sudo()
        domain = []
        state = params.get("state")
        if state:
            valid_states = dict(Request._fields["state"].selection)
            if state not in valid_states:
                return return_Response(
                    message="'state' must be one of %s." % list(valid_states),
                    status=400,
                )
            domain.append(("state", "=", state))
        for key, field in (
            ("batch_id", "batch_id"),
            ("budget_id", "project_budget_id"),
            ("project_id", "project_id"),
        ):
            if params.get(key):
                try:
                    domain.append((field, "=", int(params.get(key))))
                except (TypeError, ValueError):
                    return return_Response(
                        message="'%s' must be an integer." % key, status=400,
                    )
        search = (params.get("search") or "").strip()
        if search:
            domain.append(("name", "ilike", search))
        total = Request.search_count(domain)
        requests = Request.search(
            domain, order="request_date desc, id desc", limit=limit, offset=offset,
        )
        llm_cost_cache = {}
        return return_Response(
            message="OK", status=200,
            data={"data": {
                "total": total, "limit": limit, "offset": offset,
                "records": [
                    _request_to_dict(r, llm_cost_cache=llm_cost_cache)
                    for r in requests
                ],
            }},
        )

    # POST — create a batch budget request AND send it for approval (pending).
    #   Mirrors the Odoo wizard (etp.batch.budget.request.wizard.action_submit).
    #   body: {
    #     "batch_id": <int>, "justification": "<text>",
    #     "total_tasks": <int>, "buffer_pct": <num>, "priority": "low|normal|high|urgent",
    #     "subject": "<text>", "message": "<html/text>",
    #     "model_lines": [ {"ai_model_id": int, "description": str, "per_task_cost": num} ],
    #     "infra_lines": [ {"infra_type_id": int, "description": str, "requested_amount": num} ],
    #     "attachment_ids": [int]
    #   }
    @http.route(
        "/api/v1/etp_projects/budget_request/create",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def budget_request_create(self, **params):
        jdata = _read_json_body()

        batch_id = jdata.get("batch_id")
        if not isinstance(batch_id, int):
            return return_Response(
                message="'batch_id' is required and must be an integer.", status=400,
            )
        batch = request.env["etp.batch.budget"].sudo().browse(batch_id)
        if not batch.exists():
            return return_Response(
                message="Phase %s not found." % batch_id, status=400,
            )

        justification = (jdata.get("justification") or "").strip()
        if not justification:
            return return_Response(
                message="'justification' is required.", status=400,
            )

        total_tasks = jdata.get("total_tasks") or 0
        buffer_pct = float(jdata.get("buffer_pct") or 0.0)

        # Build wizard line commands, validating referenced ids up front.
        # requested_amount is set explicitly: wizard onchange does not fire on server-side create().
        model_lines = jdata.get("model_lines") or []
        model_cmds = []
        for line in model_lines:
            ai_model_id = line.get("ai_model_id")
            if not isinstance(ai_model_id, int):
                return return_Response(
                    message="Each model line needs an integer 'ai_model_id'.",
                    status=400,
                )
            per_task_cost = float(line.get("per_task_cost") or 0.0)
            if "requested_amount" in line:
                requested_amount = float(line.get("requested_amount") or 0.0)
            else:
                requested_amount = (total_tasks or 0) * per_task_cost
            model_cmds.append((0, 0, {
                "ai_model_id": ai_model_id,
                "description": line.get("description") or False,
                "per_task_cost": per_task_cost,
                "requested_amount": requested_amount,
            }))
        missing = _missing_ids(
            "etp.ai.model", [c[2]["ai_model_id"] for c in model_cmds],
        )
        if missing:
            return return_Response(
                message="Model(s) not found: %s" % missing, status=400,
            )

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
                "description": line.get("description") or False,
                "requested_amount": float(line.get("requested_amount") or 0.0),
            }))
        missing = _missing_ids(
            "etp.infra.type", [c[2]["infra_type_id"] for c in infra_cmds],
        )
        if missing:
            return return_Response(
                message="Infrastructure type(s) not found: %s" % missing, status=400,
            )

        if not (model_cmds or infra_cmds):
            return return_Response(
                message="Add at least one model or infrastructure line.", status=400,
            )

        # requested_total is set explicitly: wizard onchange does not fire on server-side create().
        if "requested_total" in jdata:
            requested_total = float(jdata.get("requested_total") or 0.0)
        else:
            subtotal = (
                sum(c[2]["requested_amount"] for c in model_cmds)
                + sum(c[2]["requested_amount"] for c in infra_cmds)
            )
            requested_total = subtotal * (1.0 + (buffer_pct / 100.0))

        wiz_vals = {
            "batch_id": batch_id,
            "justification": justification,
            "subject": jdata.get("subject") or False,
            "message": jdata.get("message") or False,
            "priority": jdata.get("priority") or "normal",
            "total_tasks": total_tasks,
            "buffer_pct": buffer_pct,
            "requested_total": requested_total,
            "model_line_ids": model_cmds,
            "infra_line_ids": infra_cmds,
        }
        attachment_ids = jdata.get("attachment_ids") or []
        if attachment_ids:
            if not all(isinstance(x, int) for x in attachment_ids):
                return return_Response(
                    message="'attachment_ids' must be a list of integers.", status=400,
                )
            wiz_vals["attachment_ids"] = [(6, 0, attachment_ids)]

        try:
            wizard = request.env["etp.batch.budget.request.wizard"].sudo().create(
                wiz_vals
            )
            result = wizard.action_submit()
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("budget_request_create failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

        # action_submit returns an act_window dict pointing at the new request.
        new_id = result.get("res_id") if isinstance(result, dict) else None
        req = request.env[REQUEST_MODEL].sudo().browse(new_id) if new_id else None
        if not req or not req.exists():
            return return_Response(
                message="Request submitted but could not be located.", status=400,
            )
        return return_Response(
            message="Budget request submitted for approval.",
            status=200,
            data={"data": _request_to_dict(req)},
        )

    # POST — approve or reject a batch budget request.
    #   body: { "request_id": <int>, "action": "approve"|"reject",
    #           "rejection_reason": "<text, required for reject>" }
    @http.route(
        "/api/v1/etp_projects/budget_request/action",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def budget_request_action(self, **params):
        jdata = _read_json_body()
        request_id = jdata.get("request_id")
        action = (jdata.get("action") or "").strip().lower()

        if not isinstance(request_id, int):
            return return_Response(
                message="'request_id' is required and must be an integer.",
                status=400,
            )
        if action not in ("approve", "reject"):
            return return_Response(
                message="'action' must be 'approve' or 'reject'.", status=400,
            )

        req = request.env[REQUEST_MODEL].sudo().browse(request_id)
        if not req.exists():
            return return_Response(
                message="Budget request %s not found." % request_id, status=404,
            )

        try:
            if action == "approve":
                if jdata.get("approved_total"):
                    req.approved_total = float(jdata.get("approved_total"))
                req.action_approve()
            else:
                reason = (jdata.get("rejection_reason") or "").strip()
                if not reason:
                    return return_Response(
                        message="'rejection_reason' is required to reject.",
                        status=400,
                    )
                req._do_reject(reason)
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("budget_request_action failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

        verb = "approved" if action == "approve" else "rejected"
        return return_Response(
            message="Budget request %s." % verb,
            status=200,
            data={"data": _request_to_dict(req)},
        )

    # POST — create a project budget (whole 5-step wizard in one call)
    @http.route(
        "/api/v1/etp_projects/project_budget/create",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_create(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()

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
        default_approver_ids = request.env[BUDGET_MODEL].sudo()._get_default_approver_user_ids()
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
        final_approver_ids = list(dict.fromkeys(default_approver_ids + approver_ids))
        if final_approver_ids:
            vals["approver_user_ids"] = [(6, 0, final_approver_ids)]

        # Step 3 — Models
        model_lines = jdata.get("model_lines") or []
        line_cmds = []
        for line in model_lines:
            parsed, err = _parse_model_line(line)
            if err:
                return return_Response(message=err, status=400)
            line_cmds.append((0, 0, parsed))
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

        # Step 4.5 — Subscriptions
        subscription_lines = jdata.get("subscription_lines") or []
        sub_cmds = []
        sub_cost_updates = []
        for line in subscription_lines:
            parsed, cost, err = _parse_subscription_line(line)
            if err:
                return return_Response(message=err, status=400)
            sub_cmds.append((0, 0, parsed))
            if cost is not None:
                sub_cost_updates.append((parsed["subscription_id"], cost))
        missing = _missing_ids(
            "etp.subscription",
            [c[2]["subscription_id"] for c in sub_cmds],
        )
        if missing:
            return return_Response(
                message="Subscription(s) not found: %s" % missing, status=400,
            )
        sub_assigned_ids = sorted({
            uid
            for c in sub_cmds
            for uid in c[2]["assigned_user_ids"][0][2]
        })
        missing_users = _missing_ids("res.users", sub_assigned_ids)
        if missing_users:
            return return_Response(
                message="Assigned user(s) not found: %s" % missing_users,
                status=400,
            )
        if sub_cmds:
            vals["subscription_line_ids"] = sub_cmds

        provided_links = jdata.get("attachment_ids") or []
        if provided_links and not (
            isinstance(provided_links, list)
            and all(isinstance(x, str) for x in provided_links)
        ):
            return return_Response(
                message="'attachment_ids' must be a list of S3 URL strings.",
                status=400,
            )
        uploaded_links = _upload_attachments_to_s3(uploaded_files)
        merged_links = _serialize_attachment_links(list(provided_links) + uploaded_links)
        if merged_links:
            vals["attachment_ids"] = merged_links

        try:
            for sub_id, new_cost in sub_cost_updates:
                request.env["etp.subscription"].sudo().browse(sub_id).write(
                    {"cost": new_cost},
                )
            budget = request.env[BUDGET_MODEL].sudo().create(vals)
        except (UserError, ValidationError) as e:
            request.env.cr.rollback()
            return return_Response(message=str(e), status=400)
        except Exception as e:
            request.env.cr.rollback()
            _logger.exception("project_budget_create failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

        # Step 5 — Batches (one or many) + auto-submitted budget requests (best-effort)
        buffer_pct = float(vals.get("buffer_pct") or 0.0)
        budget_amount = float(vals.get("budget_amount") or 0.0)
        raw_batches = jdata.get("batches")
        if isinstance(raw_batches, list):
            batch_inputs = raw_batches
        else:
            batch_inputs = [{
                "task_count": jdata.get("task_count"),
                "start_date": jdata.get("start_date"),
                "end_date": jdata.get("end_date"),
                "justification": jdata.get("justification"),
                "priority": jdata.get("priority"),
                "buffer_pct": buffer_pct,
            }]

        batches = []
        reqs = []
        for raw_batch in batch_inputs:
            spec, err = _normalize_batch_input(raw_batch, fallback_buffer_pct=buffer_pct)
            if err:
                return return_Response(message=err, status=400)
            try:
                with request.env.cr.savepoint():
                    batch, req = _create_batch_with_request(
                        budget, spec, budget_amount=budget_amount,
                    )
            except Exception as bexc:
                _logger.warning(
                    "project_budget_create: batch creation skipped for budget %s: %s",
                    budget.id, bexc,
                )
                continue
            if batch and batch.exists():
                batches.append(batch)
            if req and req.exists():
                reqs.append(req)

        inner_data = _budget_to_dict(budget)
        inner_data["batches"] = [_batch_summary(b) for b in batches]
        inner_data["requests"] = [_request_to_dict(r) for r in reqs]

        if reqs:
            message = "Project budget, %d phase(s) and %d budget request(s) created." % (
                len(batches), len(reqs),
            )
        elif batches:
            message = "Project budget and %d phase(s) created." % len(batches)
        else:
            message = "Project budget created."

        return return_Response(
            message=message, status=200, data={"data": inner_data},
        )

    # PATCH — update a project budget
    @http.route(
        "/api/v1/etp_projects/project_budget/update",
        methods=["PATCH"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def project_budget_update(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
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
                parsed, err = _parse_model_line(line)
                if err:
                    return return_Response(message=err, status=400)
                line_cmds.append((0, 0, parsed))
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

        # Step 4.5 — Subscriptions (send the key to replace all subscription lines)
        sub_cost_updates = []
        if "subscription_lines" in jdata:
            sub_cmds = [(5, 0, 0)]
            for line in jdata.get("subscription_lines") or []:
                parsed, cost, err = _parse_subscription_line(line)
                if err:
                    return return_Response(message=err, status=400)
                sub_cmds.append((0, 0, parsed))
                if cost is not None:
                    sub_cost_updates.append((parsed["subscription_id"], cost))
            missing = _missing_ids(
                "etp.subscription",
                [c[2]["subscription_id"] for c in sub_cmds[1:]],
            )
            if missing:
                return return_Response(
                    message="Subscription(s) not found: %s" % missing, status=400,
                )
            sub_assigned_ids = sorted({
                uid
                for c in sub_cmds[1:]
                for uid in c[2]["assigned_user_ids"][0][2]
            })
            missing_users = _missing_ids("res.users", sub_assigned_ids)
            if missing_users:
                return return_Response(
                    message="Assigned user(s) not found: %s" % missing_users,
                    status=400,
                )
            vals["subscription_line_ids"] = sub_cmds

        has_attachment_key = "attachment_ids" in jdata
        if has_attachment_key or uploaded_files:
            provided_links = jdata.get("attachment_ids") if has_attachment_key else []
            if provided_links is None:
                provided_links = []
            if not (
                isinstance(provided_links, list)
                and all(isinstance(x, str) for x in provided_links)
            ):
                return return_Response(
                    message="'attachment_ids' must be a list of S3 URL strings.",
                    status=400,
                )
            uploaded_links = _upload_attachments_to_s3(uploaded_files)
            append = bool(jdata.get("append_attachments"))
            base = _parse_attachment_links(budget.attachment_ids) if append else []
            vals["attachment_ids"] = _serialize_attachment_links(
                base + list(provided_links) + uploaded_links,
            )

        try:
            for sub_id, new_cost in sub_cost_updates:
                request.env["etp.subscription"].sudo().browse(sub_id).write(
                    {"cost": new_cost},
                )
            budget.write(vals)
        except (UserError, ValidationError) as e:
            request.env.cr.rollback()
            return return_Response(message=str(e), status=400)
        except Exception:
            request.env.cr.rollback()
            _logger.exception("project_budget_update failed")
            return return_Response(
                message="Failed to update project budget.", status=500,
            )
        return return_Response(
            message="Project budget updated.", status=200,
            data={"data": _budget_to_dict(budget)},
        )
