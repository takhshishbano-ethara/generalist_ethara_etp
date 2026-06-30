"""Project Budget REST controller (v2).

Lives under `/api/v1/etp_projects/budget/*` and is intentionally separate from
`controllers/project_budget.py`. The legacy controller stays online for
existing consumers; this module implements the simplified payload shape the
client app now uses:

    {
        "project_id": <int>,
        "budget_type": "rnd" | "operations",
        "total_no_of_tasks": <int>,
        "description": <str>,
        "approver_ids": [<int>, ...],
        "batches": [{"start_date", "end_date", "no_of_task"}, ...],
        "models": [{"model_id", "cost_type",
                    "per_task_cost", "per_trajectory_cost", "no_of_trajectory"}, ...],
        "subscription": [{"subscription_id", "cost", "assigned_to": [<int>, ...]}, ...],
        "infra": [{"infra_id", "cost"}, ...],
        "attachments": <multipart files>
    }

The total budget envelope is computed server-side from the line items so the
caller does not need to supply it.

Each project budget also gets ONE detailed professional HTML mail sent to the
combined approver pool when it is first created. The mail is posted on the
project chatter via `project._etp_post_budget_message` so it shares the same
email-client thread as every other budget event for that project.
"""
import base64
import json
import logging

from odoo import _, fields, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    generate_s3_link,
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

BUDGET_MODEL = "etp.project.aws.budget"
BATCH_MODEL = "etp.batch.budget"
PROJECT_MODEL = "project.project"
AI_MODEL = "etp.ai.model"
INFRA_MODEL = "etp.infra.type"
SUBSCRIPTION_MODEL = "etp.subscription"
VALID_PRIORITIES = ("low", "normal", "high", "urgent")

ATTACHMENT_PREFIX = "etp_projects/project_budget"

VALID_COST_TYPES = ("per_task", "per_trajectory")
CREATION_MAIL_TEMPLATE = "etp_projects.mail_template_project_budget_created"


def _read_multipart_or_json():
    """Return (parsed_json_dict, list_of_uploaded_files).

    The client may POST either:
      * multipart/form-data with a `payload` field containing the JSON body
        and a `attachments` field containing one or more file objects, or
      * application/json (no files).
    """
    content_type = (request.httprequest.content_type or "").lower()
    if content_type.startswith("multipart/form-data"):
        form = request.httprequest.form
        files = request.httprequest.files.getlist("attachments") or []
        raw_payload = form.get("payload") or "{}"
        try:
            jdata = json.loads(raw_payload) if raw_payload else {}
        except ValueError:
            jdata = {}
        return jdata or {}, files
    try:
        raw = request.httprequest.get_data(as_text=True) or ""
        jdata = json.loads(raw) if raw else {}
    except ValueError:
        jdata = {}
    return jdata or {}, []


def _upload_attachments_to_s3(files):
    urls = []
    for f in files or []:
        try:
            data = f.read()
        except Exception:
            _logger.exception("Could not read uploaded file %s", getattr(f, "filename", "?"))
            continue
        if not data:
            continue
        b64 = base64.b64encode(data).decode("utf-8")
        filename = getattr(f, "filename", "") or "attachment.bin"
        try:
            url = generate_s3_link(b64, ATTACHMENT_PREFIX, filename)
        except Exception:
            _logger.exception("S3 upload failed for %s", filename)
            continue
        if url:
            urls.append(url)
    return urls


def _parse_attachment_links(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return []


def _serialize_attachment_links(urls):
    seen = []
    for u in urls or []:
        if u and u not in seen:
            seen.append(u)
    return ",".join(seen)


def _coerce_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_date(value):
    if not value:
        return False
    if isinstance(value, str):
        try:
            return fields.Date.from_string(value)
        except Exception:
            return False
    return value


def _missing_ids(model, ids):
    if not ids:
        return []
    found = request.env[model].sudo().browse(list(ids)).exists().ids
    return [i for i in ids if i not in found]


def _pagination(params, default_limit=100, max_limit=500):
    limit = _coerce_int(params.get("limit"), default_limit) or default_limit
    offset = _coerce_int(params.get("offset"), 0) or 0
    if limit <= 0:
        return None, None, return_Response(
            message="limit must be positive.", status=400, data={},
            errors={"limit": "invalid"},
        )
    if limit > max_limit:
        limit = max_limit
    if offset < 0:
        return None, None, return_Response(
            message="offset must be >= 0.", status=400, data={},
            errors={"offset": "invalid"},
        )
    return limit, offset, None


def _budget_type_options():
    selection = request.env[BUDGET_MODEL]._fields["project_type"].selection
    return [{"value": v, "label": l} for v, l in selection]


def _budget_type_label(value):
    return dict(request.env[BUDGET_MODEL]._fields["project_type"].selection).get(value, value)


def _state_label(value):
    return dict(request.env[BUDGET_MODEL]._fields["state"].selection).get(value, value)


def _parse_model_entry(entry, idx, allow_zero_cost=False):
    if not isinstance(entry, dict):
        return None, 0.0, f"models[{idx}] must be an object."
    model_id = _coerce_int(entry.get("model_id") or entry.get("ai_model_id"))
    if not model_id:
        return None, 0.0, f"models[{idx}].model_id is required."
    cost_type = entry.get("cost_type") or "per_task"
    if cost_type not in VALID_COST_TYPES:
        return None, 0.0, (
            f"models[{idx}].cost_type must be one of {list(VALID_COST_TYPES)}."
        )
    per_task_cost = _coerce_float(entry.get("per_task_cost"), 0.0)
    per_trajectory_cost = _coerce_float(entry.get("per_trajectory_cost"), 0.0)
    iterations = _coerce_int(
        entry.get("no_of_trajectory") or entry.get("iterations"), 0
    ) or 0
    if cost_type == "per_trajectory":
        if not allow_zero_cost and (per_trajectory_cost <= 0.0 or iterations <= 0):
            return None, 0.0, (
                f"models[{idx}] requires positive per_trajectory_cost and "
                f"no_of_trajectory when cost_type='per_trajectory'."
            )
        per_task_cost = per_trajectory_cost * iterations
    else:
        if not allow_zero_cost and per_task_cost <= 0.0:
            return None, 0.0, (
                f"models[{idx}].per_task_cost must be > 0 for cost_type='per_task'."
            )
    vals = {
        "ai_model_id": model_id,
        "cost_type": cost_type,
        "per_task_cost": per_task_cost,
        "per_trajectory_cost": per_trajectory_cost,
        "iterations": iterations,
    }
    return vals, per_task_cost, None


def _parse_infra_entry(entry, idx):
    if not isinstance(entry, dict):
        return None, 0.0, f"infra[{idx}] must be an object."
    infra_id = _coerce_int(entry.get("infra_id") or entry.get("infra_type_id"))
    if not infra_id:
        return None, 0.0, f"infra[{idx}].infra_id is required."
    cost = _coerce_float(entry.get("cost") or entry.get("budget_amount"), 0.0)
    if cost < 0.0:
        return None, 0.0, f"infra[{idx}].cost must be >= 0."
    vals = {
        "infra_type_id": infra_id,
        "budget_amount": cost,
        "description": (entry.get("description") or "").strip() or False,
    }
    return vals, cost, None


def _parse_subscription_entry(entry, idx):
    """Parse one subscription entry.

    Returns (vals_dict, monthly_total_float, cost_for_catalog_update_or_None, error_or_None).
    `cost_for_catalog_update` is set when the caller provided an explicit
    per-seat `cost`; the create endpoint writes it back to `etp.subscription.cost`
    BEFORE creating the budget so the related `cost_per_subscription` on the
    line reflects the new value.
    """
    if not isinstance(entry, dict):
        return None, 0.0, None, f"subscription[{idx}] must be an object."
    sub_id = _coerce_int(entry.get("subscription_id"))
    if not sub_id:
        return None, 0.0, None, f"subscription[{idx}].subscription_id is required."
    assigned_to = entry.get("assigned_to") or entry.get("assigned_user_ids") or []
    if not isinstance(assigned_to, (list, tuple)):
        return None, 0.0, None, (
            f"subscription[{idx}].assigned_to must be a list of user ids."
        )
    assigned_ids = []
    for uid in assigned_to:
        coerced = _coerce_int(uid)
        if coerced:
            assigned_ids.append(coerced)
    cost_for_catalog = None
    if "cost" in entry and entry.get("cost") not in (None, ""):
        cost_for_catalog = _coerce_float(entry.get("cost"), 0.0)
        if cost_for_catalog < 0.0:
            return None, 0.0, None, f"subscription[{idx}].cost must be >= 0."
    monthly_total = (cost_for_catalog or 0.0) * len(assigned_ids)
    vals = {
        "subscription_id": sub_id,
        "assigned_user_ids": [(6, 0, assigned_ids)],
    }
    return vals, monthly_total, cost_for_catalog, None


def _parse_batch_entry(entry, idx, fallback_buffer=0.0):
    if not isinstance(entry, dict):
        return None, 0, f"batches[{idx}] must be an object."
    no_of_task = _coerce_int(entry.get("no_of_task") or entry.get("total_tasks"), 0) or 0
    if no_of_task <= 0:
        return None, 0, f"batches[{idx}].no_of_task must be > 0."
    start_date = _coerce_date(entry.get("start_date"))
    end_date = _coerce_date(entry.get("end_date"))
    if not start_date or not end_date:
        return None, 0, f"batches[{idx}] requires both start_date and end_date."
    if end_date < start_date:
        return None, 0, f"batches[{idx}].end_date cannot precede start_date."
    vals = {
        "total_tasks": no_of_task,
        "start_date": start_date,
        "end_date": end_date,
        "buffer_pct": _coerce_float(entry.get("buffer_pct"), fallback_buffer),
    }
    if entry.get("name"):
        vals["name"] = str(entry["name"]).strip()
    return vals, no_of_task, None


def _compute_budget_amount(total_tasks, model_costs, infra_costs, subscription_totals):
    """Server-side budget envelope.

    model_total = total_tasks * sum(per_task_cost across models)
    infra_total = sum(infra.cost)
    subscription_total = sum(cost_per_seat * len(assigned_to))
    budget_amount = sum of the three.
    """
    model_total = (total_tasks or 0) * sum(model_costs or [])
    infra_total = sum(infra_costs or [])
    sub_total = sum(subscription_totals or [])
    return round(model_total + infra_total + sub_total, 2)


def _budget_to_dict(budget):
    budget.ensure_one()
    model_lines = []
    for ln in budget.model_line_ids:
        model_lines.append({
            "id": ln.id,
            "ai_model_id": ln.ai_model_id.id,
            "model_name": ln.ai_model_id.display_name or ln.ai_model_id.name,
            "provider": getattr(ln.ai_model_id, "provider", "") or "",
            "cost_type": ln.cost_type,
            "per_task_cost": ln.per_task_cost,
            "per_trajectory_cost": ln.per_trajectory_cost,
            "iterations": ln.iterations,
        })
    infra_lines = []
    for ln in budget.infra_line_ids:
        infra_lines.append({
            "id": ln.id,
            "infra_id": ln.infra_type_id.id,
            "infra_name": ln.infra_type_id.display_name or ln.infra_type_id.name,
            "description": ln.description or "",
            "cost": ln.budget_amount,
            "per_day_cost": ln.per_day_cost,
        })
    sub_lines = []
    for ln in budget.subscription_line_ids:
        sub_lines.append({
            "id": ln.id,
            "subscription_id": ln.subscription_id.id,
            "subscription_name": ln.subscription_id.display_name or ln.subscription_id.name,
            "cost_per_seat": ln.cost_per_subscription,
            "assigned_to": ln.assigned_user_ids.ids,
            "seat_count": ln.subscription_count,
            "monthly_total": ln.final_amount,
            "per_day_cost": ln.per_day_cost,
        })
    batches = []
    for b in budget.batch_budget_ids:
        batches.append({
            "id": b.id,
            "name": b.name,
            "total_tasks": b.total_tasks,
            "start_date": b.start_date.isoformat() if b.start_date else None,
            "end_date": b.end_date.isoformat() if b.end_date else None,
            "buffer_pct": b.buffer_pct,
            "state": b.state,
        })
    return {
        "id": budget.id,
        "name": budget.name,
        "description": budget.description or "",
        "project_id": budget.project_id.id,
        "project_name": budget.project_id.display_name,
        "budget_type": budget.project_type,
        "budget_type_label": _budget_type_label(budget.project_type),
        "state": budget.state,
        "state_label": _state_label(budget.state),
        "priority": budget.priority,
        "priority_label": dict(budget._fields["priority"].selection).get(
            budget.priority, budget.priority or "",
        ) if budget.priority else "",
        "total_tasks": budget.total_tasks,
        "buffer_pct": budget.buffer_pct,
        "budget_amount": budget.budget_amount,
        "approver_ids": budget.approver_user_ids.ids,
        "approvers": [
            {"id": u.id, "name": u.name, "email": u.partner_id.email or ""}
            for u in budget.approver_user_ids
        ],
        "model_lines": model_lines,
        "infra_lines": infra_lines,
        "subscription_lines": sub_lines,
        "batches": batches,
        "attachment_ids": _parse_attachment_links(budget.attachment_ids),
        "create_date": fields.Datetime.to_string(budget.create_date) if budget.create_date else None,
        "write_date": fields.Datetime.to_string(budget.write_date) if budget.write_date else None,
    }


def _budget_to_summary(budget):
    return {
        "id": budget.id,
        "name": budget.name,
        "project_id": budget.project_id.id,
        "project_name": budget.project_id.display_name,
        "budget_type": budget.project_type,
        "budget_type_label": _budget_type_label(budget.project_type),
        "state": budget.state,
        "state_label": _state_label(budget.state),
        "total_tasks": budget.total_tasks,
        "budget_amount": budget.budget_amount,
        "batch_count": len(budget.batch_budget_ids),
        "approver_count": len(budget.approver_user_ids),
        "create_date": fields.Datetime.to_string(budget.create_date) if budget.create_date else None,
    }


def _send_creation_mail(budget):
    project = budget.project_id
    if not project:
        return False
    partner_ids = budget.approver_user_ids.mapped("partner_id").ids
    cto = getattr(budget, "cto_user_id", False)
    if cto and cto.partner_id and cto.partner_id.id not in partner_ids:
        partner_ids.append(cto.partner_id.id)
    if not partner_ids:
        return False
    try:
        return project._etp_post_budget_message(
            CREATION_MAIL_TEMPLATE, budget, partner_ids,
        )
    except Exception:
        _logger.exception(
            "Failed to post project budget creation mail for budget %s", budget.id,
        )
        return False


REQUEST_MODEL = "etp.batch.budget.request"


def _request_state_label(value):
    selection = request.env[REQUEST_MODEL]._fields["state"].selection
    return dict(selection).get(value, value)


def _user_brief(user):
    if not user:
        return None
    return {
        "id": user.id,
        "name": user.name or "",
        "email": user.email or (user.partner_id.email if user.partner_id else "") or "",
    }


def _batch_short(batch):
    if not batch:
        return None
    return {
        "id": batch.id,
        "name": batch.name,
        "state": batch.state,
        "total_tasks": batch.total_tasks,
        "done_tasks": batch.done_tasks,
        "remaining_tasks": batch.remaining_tasks,
        "start_date": batch.start_date.isoformat() if batch.start_date else None,
        "end_date": batch.end_date.isoformat() if batch.end_date else None,
    }


def _batch_detail(batch):
    if not batch:
        return None
    model_lines = [
        {
            "id": ln.id,
            "ai_model_id": ln.ai_model_id.id,
            "model_name": ln.ai_model_id.display_name or ln.ai_model_id.name,
            "cost_type": ln.cost_type,
            "per_task_cost": ln.per_task_cost,
            "per_trajectory_cost": ln.per_trajectory_cost,
            "iterations": ln.iterations,
        }
        for ln in batch.model_line_ids
    ]
    infra_lines = [
        {
            "id": ln.id,
            "infra_id": ln.infra_type_id.id,
            "infra_name": ln.infra_type_id.display_name or ln.infra_type_id.name,
            "description": ln.description or "",
            "cost": ln.budget_amount,
            "per_day_cost": ln.per_day_cost,
            "start_date": ln.start_date.isoformat() if ln.start_date else None,
            "end_date": ln.end_date.isoformat() if ln.end_date else None,
        }
        for ln in batch.infra_line_ids
    ]
    sub_lines = [
        {
            "id": ln.id,
            "subscription_id": ln.subscription_id.id,
            "subscription_name": ln.subscription_id.display_name or ln.subscription_id.name,
            "cost_per_seat": ln.cost_per_subscription,
            "assigned_to": ln.assigned_user_ids.ids,
            "seat_count": ln.subscription_count,
            "monthly_total": ln.final_amount,
            "per_day_cost": ln.per_day_cost,
            "approved_amount": ln.approved_amount,
        }
        for ln in batch.subscription_line_ids
    ]
    short = _batch_short(batch)
    short.update({
        "buffer_pct": batch.buffer_pct,
        "estimated_cost": batch.estimated_cost,
        "batch_budget": batch.batch_budget,
        "approved_amount": batch.approved_amount,
        "consumed_cost": batch.consumed_cost,
        "consumed_pct": batch.consumed_pct,
        "remaining_cost": batch.remaining_cost,
        "model_lines": model_lines,
        "infra_lines": infra_lines,
        "subscription_lines": sub_lines,
    })
    return short


def _project_budget_overview(project_budget):
    if not project_budget:
        return None
    batches = project_budget.batch_budget_ids
    done_overall = sum(batches.mapped("done_tasks"))
    remaining_overall = sum(batches.mapped("remaining_tasks"))
    return {
        "id": project_budget.id,
        "name": project_budget.name,
        "total_tasks": project_budget.total_tasks,
        "done_tasks_overall": done_overall,
        "remaining_tasks_overall": remaining_overall,
        "batches": [_batch_short(b) for b in batches],
    }


def _request_to_summary(req):
    req.ensure_one()
    return {
        "id": req.id,
        "name": req.name,
        "state": req.state,
        "state_label": _request_state_label(req.state),
        "request_date": fields.Datetime.to_string(req.request_date) if req.request_date else None,
        "priority": req.priority,
        "requester": _user_brief(req.requester_id),
        "approver": _user_brief(req.approver_id),
        "batch": {
            "id": req.batch_id.id,
            "name": req.batch_id.name,
            "state": req.batch_id.state,
        } if req.batch_id else None,
        "project": {
            "id": req.project_id.id,
            "name": req.project_id.display_name,
        } if req.project_id else None,
        "project_budget_id": req.project_budget_id.id if req.project_budget_id else None,
        "total_tasks": req.total_tasks,
        "requested_total": req.requested_total,
        "approved_total": req.approved_total,
        "remaining_amount": req.remaining_amount,
        "sequence_number": req.sequence_number,
        "is_followup": req.is_followup,
        "can_follow_up": req.can_follow_up,
        "follow_up_count": req.follow_up_count,
    }


def _request_to_detail(req):
    req.ensure_one()
    model_approved_base = sum(req.model_line_ids.mapped("approved_amount"))
    infra_approved_base = sum(req.infra_line_ids.mapped("approved_amount"))
    sub_approved_base = sum(req.subscription_line_ids.mapped("approved_amount"))
    approved_base = model_approved_base + infra_approved_base + sub_approved_base
    model_requested_total = sum(req.model_line_ids.mapped("requested_amount"))
    infra_requested_total = sum(req.infra_line_ids.mapped("requested_amount"))
    sub_requested_total = sum(req.subscription_line_ids.mapped("requested_amount"))
    sub_monthly_total = sum(req.subscription_line_ids.mapped("final_amount"))
    sub_per_day_total = sum(req.subscription_line_ids.mapped("per_day_cost"))
    buffer_factor = (req.buffer_pct or 0.0) / 100.0

    def _share_pct(amount):
        return round((amount / approved_base) * 100.0, 4) if approved_base else 0.0

    model_lines = [
        {
            "id": ln.id,
            "ai_model_id": ln.ai_model_id.id,
            "model_name": ln.ai_model_id.display_name or ln.ai_model_id.name,
            "provider": getattr(ln.ai_model_id, "provider", "") or "",
            "description": ln.description or "",
            "cost_type": ln.cost_type,
            "per_task_cost": ln.per_task_cost,
            "per_trajectory_cost": ln.per_trajectory_cost,
            "iterations": ln.iterations,
            "requested_amount": ln.requested_amount,
            "approved_amount": ln.approved_amount,
            "approved_share_pct": _share_pct(ln.approved_amount or 0.0),
        }
        for ln in req.model_line_ids
    ]
    infra_lines = [
        {
            "id": ln.id,
            "infra_id": ln.infra_type_id.id,
            "infra_name": ln.infra_type_id.display_name or ln.infra_type_id.name,
            "description": ln.description or "",
            "requested_amount": ln.requested_amount,
            "approved_amount": ln.approved_amount,
            "per_day_requested": ln.per_day_requested,
            "per_day_approved": ln.per_day_approved,
            "approved_share_pct": _share_pct(ln.approved_amount or 0.0),
            "start_date": ln.start_date.isoformat() if ln.start_date else None,
            "end_date": ln.end_date.isoformat() if ln.end_date else None,
        }
        for ln in req.infra_line_ids
    ]
    sub_lines = [
        {
            "id": ln.id,
            "subscription_id": ln.subscription_id.id,
            "subscription_name": ln.subscription_id.display_name or ln.subscription_id.name,
            "cost_per_subscription": ln.cost_per_subscription,
            "assigned_to": ln.assigned_user_ids.ids,
            "seat_count": ln.subscription_count,
            "monthly_total": ln.final_amount,
            "per_day_cost": ln.per_day_cost,
            "requested_amount": ln.requested_amount,
            "approved_amount": ln.approved_amount,
            "approved_share_pct": _share_pct(ln.approved_amount or 0.0),
        }
        for ln in req.subscription_line_ids
    ]
    parent_brief = None
    if req.parent_request_id:
        parent_brief = {
            "id": req.parent_request_id.id,
            "name": req.parent_request_id.name,
            "state": req.parent_request_id.state,
        }
    detail = _request_to_summary(req)
    detail.update({
        "justification": req.justification or "",
        "subject": req.subject or "",
        "message": req.message or "",
        "attachment_ids": req.attachment_ids.ids,
        "approval_date": fields.Datetime.to_string(req.approval_date) if req.approval_date else None,
        "rejection_reason": req.rejection_reason or "",
        "buffer_pct": req.buffer_pct,
        "parent_request": parent_brief,
        "model_lines": model_lines,
        "infra_lines": infra_lines,
        "subscription_lines": sub_lines,
        "totals_breakdown": {
            "model_requested_total": model_requested_total,
            "model_approved_total": model_approved_base,
            "infra_requested_total": infra_requested_total,
            "infra_approved_total": infra_approved_base,
            "subscription_requested_total": sub_requested_total,
            "subscription_approved_total": sub_approved_base,
            "subscription_monthly_total": sub_monthly_total,
            "subscription_per_day_total": sub_per_day_total,
            "buffer_pct": req.buffer_pct,
            "buffer_amount_requested": (
                model_requested_total + infra_requested_total + sub_requested_total
            ) * buffer_factor,
            "buffer_amount_approved": approved_base * buffer_factor,
        },
        "approved_allocation": {
            "model": {
                "amount": model_approved_base,
                "share_pct": _share_pct(model_approved_base),
            },
            "infra": {
                "amount": infra_approved_base,
                "share_pct": _share_pct(infra_approved_base),
            },
            "subscription": {
                "amount": sub_approved_base,
                "share_pct": _share_pct(sub_approved_base),
                "monthly_amount": sub_monthly_total,
                "per_day_amount": sub_per_day_total,
            },
        },
        "project_budget_overview": _project_budget_overview(req.project_budget_id),
        "request_batch": {
            "short": _batch_short(req.batch_id),
            "detail": _batch_detail(req.batch_id),
        },
    })
    return detail


class EtpBudgetController(http.Controller):

    @http.route(
        "/api/v1/etp_projects/budget/models",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_ai_models(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = []
        if not _coerce_int(params.get("include_inactive"), 0):
            domain.append(("active", "=", True))
        search = (params.get("search") or "").strip()
        if search:
            domain += ["|", ("name", "ilike", search), ("provider", "ilike", search)]
        Model = request.env[AI_MODEL].sudo()
        total = Model.search_count(domain)
        records = Model.search(domain, limit=limit, offset=offset, order="sequence, name")
        items = [
            {"id": r.id, "name": r.name, "provider": r.provider or "", "active": r.active}
            for r in records
        ]
        return return_Response(
            message="AI models fetched.", status=200,
            data={"total": total, "limit": limit, "offset": offset, "models": items},
        )

    @http.route(
        "/api/v1/etp_projects/budget/infra",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_infra_types(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = []
        if not _coerce_int(params.get("include_inactive"), 0):
            domain.append(("active", "=", True))
        search = (params.get("search") or "").strip()
        if search:
            domain += ["|", ("name", "ilike", search), ("code", "ilike", search)]
        Model = request.env[INFRA_MODEL].sudo()
        total = Model.search_count(domain)
        records = Model.search(domain, limit=limit, offset=offset, order="sequence, name")
        items = [
            {"id": r.id, "name": r.name, "code": r.code or "", "active": r.active}
            for r in records
        ]
        return return_Response(
            message="Infrastructure types fetched.", status=200,
            data={"total": total, "limit": limit, "offset": offset, "infra": items},
        )

    @http.route(
        "/api/v1/etp_projects/budget/subscriptions",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_subscriptions(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = []
        if not _coerce_int(params.get("include_inactive"), 0):
            domain.append(("active", "=", True))
        search = (params.get("search") or "").strip()
        if search:
            domain.append(("name", "ilike", search))
        Model = request.env[SUBSCRIPTION_MODEL].sudo()
        total = Model.search_count(domain)
        records = Model.search(domain, limit=limit, offset=offset, order="sequence, name")
        items = [
            {
                "id": r.id,
                "name": r.name,
                "cost": r.cost,
                "per_day_cost": (r.cost or 0.0) / 30.0,
                "active": r.active,
            }
            for r in records
        ]
        return return_Response(
            message="Subscriptions fetched.", status=200,
            data={"total": total, "limit": limit, "offset": offset, "subscriptions": items},
        )

    @http.route(
        "/api/v1/etp_projects/budget/default_approvers",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_default_approvers(self, **params):
        user_ids = request.env[BUDGET_MODEL].sudo()._get_default_approver_user_ids()
        users = request.env["res.users"].sudo().browse(user_ids).exists()
        items = [
            {
                "id": u.id,
                "name": u.name or "",
                "login": u.login or "",
                "email": u.email or (u.partner_id.email if u.partner_id else "") or "",
            }
            for u in users
        ]
        return return_Response(
            message="Default project budget approvers fetched.", status=200,
            data={"total": len(items), "approvers": items},
        )

    @http.route(
        "/api/v1/etp_projects/budget/list",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_budgets(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = []
        project_id = _coerce_int(params.get("project_id"))
        if project_id:
            domain.append(("project_id", "=", project_id))
        budget_type = (params.get("budget_type") or "").strip()
        if budget_type:
            valid_types = [v for v, _ in request.env[BUDGET_MODEL]._fields["project_type"].selection]
            if budget_type not in valid_types:
                return return_Response(
                    message=f"budget_type must be one of {valid_types}.",
                    status=400, data={},
                )
            domain.append(("project_type", "=", budget_type))
        state = (params.get("state") or "").strip()
        if state:
            valid_states = [v for v, _ in request.env[BUDGET_MODEL]._fields["state"].selection]
            if state not in valid_states:
                return return_Response(
                    message=f"state must be one of {valid_states}.",
                    status=400, data={},
                )
            domain.append(("state", "=", state))
        search = (params.get("search") or "").strip()
        if search:
            domain += [
                "|", ("name", "ilike", search),
                ("description", "ilike", search),
            ]
        Model = request.env[BUDGET_MODEL].sudo()
        total = Model.search_count(domain)
        records = Model.search(domain, limit=limit, offset=offset, order="create_date desc, id desc")
        items = [_budget_to_summary(r) for r in records]
        return return_Response(
            message="Project budgets fetched.", status=200,
            data={"total": total, "limit": limit, "offset": offset, "budgets": items},
        )

    @http.route(
        "/api/v1/etp_projects/budget/detail",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def budget_detail(self, **params):
        budget_id = _coerce_int(params.get("id"))
        if not budget_id:
            return return_Response(
                message="id is required.", status=400, data={},
            )
        budget = request.env[BUDGET_MODEL].sudo().browse(budget_id).exists()
        if not budget:
            return return_Response(
                message="Project budget not found.", status=404, data={},
            )
        return return_Response(
            message="Project budget detail fetched.", status=200,
            data={"data": _budget_to_dict(budget)},
        )

    @http.route(
        "/api/v1/etp_projects/budget/create",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def create_budget(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()

        project_id = _coerce_int(jdata.get("project_id"))
        if not project_id:
            return return_Response(
                message="project_id is required.", status=400, data={},
            )
        project = request.env[PROJECT_MODEL].sudo().browse(project_id).exists()
        if not project:
            return return_Response(
                message=f"Project {project_id} does not exist.",
                status=400, data={},
            )

        budget_type = (jdata.get("budget_type") or "").strip()
        valid_types = [v for v, _ in request.env[BUDGET_MODEL]._fields["project_type"].selection]
        if budget_type not in valid_types:
            return return_Response(
                message=f"budget_type must be one of {valid_types}.",
                status=400, data={},
            )
        is_rnd = budget_type == "rnd"

        initial_budget = _coerce_float(jdata.get("budget_amount"), 0.0)
        if is_rnd and initial_budget <= 0.0:
            return return_Response(
                message="initial_budget is required and must be > 0 for R&D budgets.",
                status=400, data={},
            )

        total_tasks = _coerce_int(
            jdata.get("total_no_of_tasks") or jdata.get("total_tasks"), 0,
        ) or 0
        if total_tasks <= 0:
            return return_Response(
                message="total_no_of_tasks must be > 0.", status=400, data={},
            )

        description = (jdata.get("description") or "").strip()
        buffer_pct = _coerce_float(jdata.get("buffer_pct"), 0.0)

        priority = (jdata.get("priority") or "normal").strip()
        if priority not in VALID_PRIORITIES:
            return return_Response(
                message=f"priority must be one of {list(VALID_PRIORITIES)}.",
                status=400, data={},
            )

        dup = request.env[BUDGET_MODEL].sudo().search([
            ("project_id", "=", project_id),
            ("project_type", "=", budget_type),
        ], limit=1)
        if dup:
            return return_Response(
                message=(
                    f"A {_budget_type_label(budget_type)} budget already exists for "
                    f"project '{project.display_name}'."
                ),
                status=400, data={},
            )

        raw_approver_ids = jdata.get("approver_ids") or []
        if not isinstance(raw_approver_ids, (list, tuple)):
            return return_Response(
                message="approver_ids must be a list of user ids.",
                status=400, data={},
            )
        payload_approver_ids = [_coerce_int(x) for x in raw_approver_ids if _coerce_int(x)]
        default_approver_ids = (
            request.env[BUDGET_MODEL].sudo()._get_default_approver_user_ids() or []
        )
        approver_ids = list(dict.fromkeys(default_approver_ids + payload_approver_ids))
        if not approver_ids:
            return return_Response(
                message=(
                    "No approvers resolved. Provide approver_ids or configure "
                    "default approvers in Settings > ETP Projects."
                ),
                status=400, data={},
            )
        missing_approvers = _missing_ids("res.users", approver_ids)
        if missing_approvers:
            return return_Response(
                message=f"Approver user ids do not exist: {missing_approvers}.",
                status=400, data={},
            )

        raw_models = jdata.get("models") or []
        if not isinstance(raw_models, list):
            return return_Response(message="models must be a list.", status=400, data={})
        model_cmds, per_task_costs = [], []
        for idx, entry in enumerate(raw_models):
            vals, per_task_cost, err = _parse_model_entry(
                entry, idx, allow_zero_cost=is_rnd,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            model_cmds.append((0, 0, vals))
            per_task_costs.append(per_task_cost)
        if model_cmds:
            missing_models = _missing_ids(
                AI_MODEL, [c[2]["ai_model_id"] for c in model_cmds],
            )
            if missing_models:
                return return_Response(
                    message=f"AI model ids do not exist: {missing_models}.",
                    status=400, data={},
                )

        raw_infra = jdata.get("infra") or []
        if not isinstance(raw_infra, list):
            return return_Response(message="infra must be a list.", status=400, data={})
        infra_cmds, infra_costs = [], []
        for idx, entry in enumerate(raw_infra):
            vals, cost, err = _parse_infra_entry(entry, idx)
            if err:
                return return_Response(message=err, status=400, data={})
            infra_cmds.append((0, 0, vals))
            infra_costs.append(cost)
        if infra_cmds:
            missing_infra = _missing_ids(
                INFRA_MODEL, [c[2]["infra_type_id"] for c in infra_cmds],
            )
            if missing_infra:
                return return_Response(
                    message=f"Infrastructure type ids do not exist: {missing_infra}.",
                    status=400, data={},
                )

        raw_subs = jdata.get("subscription") or jdata.get("subscriptions") or []
        if not isinstance(raw_subs, list):
            return return_Response(
                message="subscription must be a list.", status=400, data={},
            )
        sub_cmds, sub_monthly_totals, sub_catalog_updates = [], [], []
        for idx, entry in enumerate(raw_subs):
            vals, monthly_total, cost_for_catalog, err = _parse_subscription_entry(entry, idx)
            if err:
                return return_Response(message=err, status=400, data={})
            sub_cmds.append((0, 0, vals))
            sub_monthly_totals.append(monthly_total)
            if cost_for_catalog is not None:
                sub_catalog_updates.append((vals["subscription_id"], cost_for_catalog))
        if sub_cmds:
            missing_subs = _missing_ids(
                SUBSCRIPTION_MODEL, [c[2]["subscription_id"] for c in sub_cmds],
            )
            if missing_subs:
                return return_Response(
                    message=f"Subscription ids do not exist: {missing_subs}.",
                    status=400, data={},
                )
            all_user_ids = []
            for c in sub_cmds:
                for cmd in c[2]["assigned_user_ids"]:
                    if isinstance(cmd, (list, tuple)) and len(cmd) >= 3:
                        all_user_ids.extend(cmd[2] or [])
            missing_users = _missing_ids("res.users", list(set(all_user_ids)))
            if missing_users:
                return return_Response(
                    message=f"Subscription assigned user ids do not exist: {missing_users}.",
                    status=400, data={},
                )

        if sub_cmds:
            catalog_costs = {}
            cat_ids = [c[2]["subscription_id"] for c in sub_cmds]
            for sub in request.env[SUBSCRIPTION_MODEL].sudo().browse(cat_ids):
                catalog_costs[sub.id] = sub.cost or 0.0
            updates_map = dict(sub_catalog_updates)
            for i, cmd in enumerate(sub_cmds):
                if sub_monthly_totals[i] == 0.0:
                    sub_id = cmd[2]["subscription_id"]
                    seats = 0
                    for assignment in cmd[2]["assigned_user_ids"]:
                        if isinstance(assignment, (list, tuple)) and len(assignment) >= 3:
                            seats = len(assignment[2] or [])
                    per_seat = updates_map.get(sub_id, catalog_costs.get(sub_id, 0.0))
                    sub_monthly_totals[i] = per_seat * seats

        raw_batches = jdata.get("batches") or []
        if not isinstance(raw_batches, list):
            return return_Response(
                message="batches must be a list.", status=400, data={},
            )
        batch_specs = []
        total_batch_tasks = 0
        for idx, entry in enumerate(raw_batches):
            spec, count, err = _parse_batch_entry(entry, idx, fallback_buffer=buffer_pct)
            if err:
                return return_Response(message=err, status=400, data={})
            batch_specs.append(spec)
            total_batch_tasks += count
        if batch_specs and total_batch_tasks != total_tasks:
            return return_Response(
                message=(
                    f"Sum of batch no_of_task ({total_batch_tasks}) must equal "
                    f"total_no_of_tasks ({total_tasks})."
                ),
                status=400, data={},
            )

        provided_links = _parse_attachment_links(jdata.get("attachment_ids"))
        uploaded_links = _upload_attachments_to_s3(uploaded_files) if uploaded_files else []
        attachment_links = _serialize_attachment_links(provided_links + uploaded_links)

        budget_amount = _compute_budget_amount(
            total_tasks, per_task_costs, infra_costs, sub_monthly_totals,
        )

        try:
            for sub_id, new_cost in sub_catalog_updates:
                request.env[SUBSCRIPTION_MODEL].sudo().browse(sub_id).write({"cost": new_cost})
        except (UserError, ValidationError) as e:
            request.env.cr.rollback()
            return return_Response(message=str(e), status=400, data={})
        except Exception:
            request.env.cr.rollback()
            _logger.exception("Subscription catalog update failed")
            return return_Response(
                message="Failed to update subscription catalog.",
                status=500, data={},
            )

        name = (f"{project.display_name} - {_budget_type_label(budget_type)}").strip()
        vals = {
            "name": name,
            "project_id": project_id,
            "project_type": budget_type,
            "total_tasks": total_tasks,
            "description": description or False,
            "priority": priority,
            "buffer_pct": buffer_pct,
            "budget_amount": budget_amount,
            "approver_user_ids": [(6, 0, approver_ids)],
            "model_line_ids": model_cmds,
            "infra_line_ids": infra_cmds,
            "subscription_line_ids": sub_cmds,
            "attachment_ids": attachment_links or False,
        }
        try:
            budget = request.env[BUDGET_MODEL].sudo().create(vals)
        except (UserError, ValidationError) as e:
            request.env.cr.rollback()
            return return_Response(message=str(e), status=400, data={})
        except Exception:
            request.env.cr.rollback()
            _logger.exception("Project budget create failed")
            return return_Response(
                message="Failed to create project budget.",
                status=500, data={},
            )

        batch_infra_clone = [
            (0, 0, {
                "infra_type_id": ln.infra_type_id.id,
                "description": ln.description or False,
                "budget_amount": ln.budget_amount or 0.0,
            })
            for ln in budget.infra_line_ids
        ]

        created_batches = request.env[BATCH_MODEL].sudo()
        for spec in batch_specs:
            spec["project_budget_id"] = budget.id
            if batch_infra_clone:
                spec["infra_line_ids"] = batch_infra_clone
            try:
                with request.env.cr.savepoint():
                    created_batches |= request.env[BATCH_MODEL].sudo().create(spec)
            except Exception:
                _logger.exception(
                    "Batch creation failed for spec %s on budget %s", spec, budget.id,
                )

        if created_batches:
            total_phase_days = 0
            target_budget_amount = initial_budget if is_rnd else 0.0
            for batch in created_batches:
                duration_days = 0
                if (
                    batch.start_date
                    and batch.end_date
                    and batch.end_date >= batch.start_date
                ):
                    duration_days = (batch.end_date - batch.start_date).days + 1
                total_phase_days += duration_days
                infra_per_day = sum(batch.infra_line_ids.mapped("per_day_cost"))
                target_budget_amount += duration_days * infra_per_day
                if not is_rnd:
                    per_task = sum(batch.model_line_ids.mapped("per_task_cost"))
                    target_budget_amount += (batch.total_tasks or 0) * per_task
            sub_per_day = sum(budget.subscription_line_ids.mapped("per_day_cost"))
            target_budget_amount += total_phase_days * sub_per_day
            try:
                if is_rnd:
                    budget.sudo().write({"budget_amount": initial_budget})
                else:
                    budget.sudo().write({"budget_amount": target_budget_amount})
            except Exception:
                _logger.warning(
                    "Failed to set budget_amount=%s on budget %s",
                    target_budget_amount, budget.id,
                )

        created_request = None
        if created_batches and (is_rnd or budget.model_line_ids or budget.infra_line_ids):
            first_batch = created_batches.sorted(
                lambda b: (b.start_date or fields.Date.today(), b.id)
            )[:1]
            first_batch.ensure_one()
            duration_days = 0
            if (
                first_batch.start_date
                and first_batch.end_date
                and first_batch.end_date >= first_batch.start_date
            ):
                duration_days = (
                    first_batch.end_date - first_batch.start_date
                ).days + 1
            wiz_model_cmds = [
                (0, 0, {
                    "ai_model_id": ln.ai_model_id.id,
                    "cost_type": ln.cost_type or "per_task",
                    "per_task_cost": ln.per_task_cost or 0.0,
                    "per_trajectory_cost": ln.per_trajectory_cost or 0.0,
                    "iterations": ln.iterations or 0,
                    "requested_amount": (first_batch.total_tasks or 0) * (ln.per_task_cost or 0.0),
                })
                for ln in budget.model_line_ids
            ]
            wiz_infra_cmds = [
                (0, 0, {
                    "infra_type_id": ln.infra_type_id.id,
                    "description": ln.description or False,
                    "requested_amount": duration_days * ((ln.budget_amount or 0.0) / 30.0),
                })
                for ln in budget.infra_line_ids
            ]
            buffer_factor = 1.0 + ((first_batch.buffer_pct or 0.0) / 100.0)
            estimated_base = sum(
                cmd[2]["requested_amount"] for cmd in wiz_model_cmds
            ) + sum(
                cmd[2]["requested_amount"] for cmd in wiz_infra_cmds
            )
            requested_total = initial_budget if is_rnd else estimated_base * buffer_factor
            try:
                with request.env.cr.savepoint():
                    wiz_vals = {
                        "batch_id": first_batch.id,
                        "justification": description or f"Auto-generated from project budget '{budget.name}' creation.",
                        "priority": priority,
                        "total_tasks": first_batch.total_tasks,
                        "buffer_pct": first_batch.buffer_pct,
                        "requested_total": requested_total,
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
                            created_request = candidate
            except Exception as wexc:
                _logger.warning(
                    "Initial batch budget request skipped for budget %s batch %s: %s",
                    budget.id, first_batch.id, wexc,
                )

        try:
            _send_creation_mail(budget)
        except Exception:
            _logger.exception("Creation mail post failed for budget %s", budget.id)

        data = _budget_to_dict(budget)
        if created_request:
            data["initial_request"] = {
                "id": created_request.id,
                "name": created_request.name,
                "batch_id": created_request.batch_id.id,
                "state": created_request.state,
                "requested_total": created_request.requested_total,
                "priority": created_request.priority,
            }
        message = (
            f"Project budget '{budget.name}' created with "
            f"{len(created_batches)} phase(s)."
        )
        if created_request:
            message += f" Initial request '{created_request.name}' submitted for approval."
        return return_Response(message=message, status=200, data={"data": data})


    @http.route(
        "/api/v1/etp_projects/budget/update",
        type="http", auth="none", methods=["PATCH", "POST"], csrf=False, cors="*",
    )
    @validate_token
    def update_budget(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        budget_id = _coerce_int(jdata.get("id"))
        if not budget_id:
            return return_Response(message="id is required.", status=400, data={})
        budget = request.env[BUDGET_MODEL].sudo().browse(budget_id).exists()
        if not budget:
            return return_Response(
                message="Project budget not found.", status=404, data={},
            )

        vals = {}
        recompute = False

        if "project_id" in jdata:
            project_id = _coerce_int(jdata.get("project_id"))
            if not project_id:
                return return_Response(
                    message="project_id must be a valid integer.",
                    status=400, data={},
                )
            if not request.env[PROJECT_MODEL].sudo().browse(project_id).exists():
                return return_Response(
                    message=f"Project {project_id} does not exist.",
                    status=400, data={},
                )
            vals["project_id"] = project_id

        if "budget_type" in jdata:
            budget_type = (jdata.get("budget_type") or "").strip()
            valid_types = [v for v, _ in request.env[BUDGET_MODEL]._fields["project_type"].selection]
            if budget_type not in valid_types:
                return return_Response(
                    message=f"budget_type must be one of {valid_types}.",
                    status=400, data={},
                )
            vals["project_type"] = budget_type

        if "state" in jdata:
            state = (jdata.get("state") or "").strip()
            valid_states = [v for v, _ in request.env[BUDGET_MODEL]._fields["state"].selection]
            if state not in valid_states:
                return return_Response(
                    message=f"state must be one of {valid_states}.",
                    status=400, data={},
                )
            vals["state"] = state

        if "description" in jdata:
            vals["description"] = (jdata.get("description") or "").strip() or False

        if "name" in jdata:
            new_name = (jdata.get("name") or "").strip()
            if not new_name:
                return return_Response(
                    message="name cannot be empty.", status=400, data={},
                )
            vals["name"] = new_name

        if "buffer_pct" in jdata:
            vals["buffer_pct"] = _coerce_float(jdata.get("buffer_pct"), 0.0)

        if "priority" in jdata:
            new_priority = (jdata.get("priority") or "").strip()
            if new_priority not in VALID_PRIORITIES:
                return return_Response(
                    message=f"priority must be one of {list(VALID_PRIORITIES)}.",
                    status=400, data={},
                )
            vals["priority"] = new_priority

        if "total_no_of_tasks" in jdata or "total_tasks" in jdata:
            total_tasks = _coerce_int(
                jdata.get("total_no_of_tasks") or jdata.get("total_tasks"), 0,
            ) or 0
            if total_tasks <= 0:
                return return_Response(
                    message="total_no_of_tasks must be > 0.",
                    status=400, data={},
                )
            vals["total_tasks"] = total_tasks
            recompute = True

        if "approver_ids" in jdata:
            raw_approver_ids = jdata.get("approver_ids") or []
            if not isinstance(raw_approver_ids, (list, tuple)):
                return return_Response(
                    message="approver_ids must be a list of user ids.",
                    status=400, data={},
                )
            payload_approver_ids = [_coerce_int(x) for x in raw_approver_ids if _coerce_int(x)]
            default_approver_ids = (
                request.env[BUDGET_MODEL].sudo()._get_default_approver_user_ids() or []
            )
            approver_ids = list(dict.fromkeys(default_approver_ids + payload_approver_ids))
            missing_approvers = _missing_ids("res.users", approver_ids)
            if missing_approvers:
                return return_Response(
                    message=f"Approver user ids do not exist: {missing_approvers}.",
                    status=400, data={},
                )
            vals["approver_user_ids"] = [(6, 0, approver_ids)]

        if "models" in jdata:
            raw_models = jdata.get("models") or []
            if not isinstance(raw_models, list):
                return return_Response(
                    message="models must be a list.", status=400, data={},
                )
            cmds = [(5, 0, 0)]
            for idx, entry in enumerate(raw_models):
                line_vals, _per, err = _parse_model_entry(entry, idx)
                if err:
                    return return_Response(message=err, status=400, data={})
                cmds.append((0, 0, line_vals))
            mids = [c[2]["ai_model_id"] for c in cmds if c[0] == 0]
            missing = _missing_ids(AI_MODEL, mids)
            if missing:
                return return_Response(
                    message=f"AI model ids do not exist: {missing}.",
                    status=400, data={},
                )
            vals["model_line_ids"] = cmds
            recompute = True

        if "infra" in jdata:
            raw_infra = jdata.get("infra") or []
            if not isinstance(raw_infra, list):
                return return_Response(
                    message="infra must be a list.", status=400, data={},
                )
            cmds = [(5, 0, 0)]
            for idx, entry in enumerate(raw_infra):
                line_vals, _c, err = _parse_infra_entry(entry, idx)
                if err:
                    return return_Response(message=err, status=400, data={})
                cmds.append((0, 0, line_vals))
            iids = [c[2]["infra_type_id"] for c in cmds if c[0] == 0]
            missing = _missing_ids(INFRA_MODEL, iids)
            if missing:
                return return_Response(
                    message=f"Infrastructure type ids do not exist: {missing}.",
                    status=400, data={},
                )
            vals["infra_line_ids"] = cmds
            recompute = True

        sub_catalog_updates = []
        if "subscription" in jdata or "subscriptions" in jdata:
            raw_subs = jdata.get("subscription") or jdata.get("subscriptions") or []
            if not isinstance(raw_subs, list):
                return return_Response(
                    message="subscription must be a list.", status=400, data={},
                )
            cmds = [(5, 0, 0)]
            for idx, entry in enumerate(raw_subs):
                line_vals, _m, cost_for_catalog, err = _parse_subscription_entry(entry, idx)
                if err:
                    return return_Response(message=err, status=400, data={})
                cmds.append((0, 0, line_vals))
                if cost_for_catalog is not None:
                    sub_catalog_updates.append((line_vals["subscription_id"], cost_for_catalog))
            sids = [c[2]["subscription_id"] for c in cmds if c[0] == 0]
            missing = _missing_ids(SUBSCRIPTION_MODEL, sids)
            if missing:
                return return_Response(
                    message=f"Subscription ids do not exist: {missing}.",
                    status=400, data={},
                )
            all_user_ids = []
            for c in cmds:
                if c[0] != 0:
                    continue
                for cmd in c[2]["assigned_user_ids"]:
                    if isinstance(cmd, (list, tuple)) and len(cmd) >= 3:
                        all_user_ids.extend(cmd[2] or [])
            missing_users = _missing_ids("res.users", list(set(all_user_ids)))
            if missing_users:
                return return_Response(
                    message=f"Subscription assigned user ids do not exist: {missing_users}.",
                    status=400, data={},
                )
            vals["subscription_line_ids"] = cmds
            recompute = True

        replace_batches = None
        if "batches" in jdata:
            raw_batches = jdata.get("batches") or []
            if not isinstance(raw_batches, list):
                return return_Response(
                    message="batches must be a list.", status=400, data={},
                )
            new_total_tasks = vals.get("total_tasks", budget.total_tasks)
            batch_specs = []
            total_batch_tasks = 0
            fallback_buffer = vals.get("buffer_pct", budget.buffer_pct)
            for idx, entry in enumerate(raw_batches):
                spec, count, err = _parse_batch_entry(entry, idx, fallback_buffer=fallback_buffer)
                if err:
                    return return_Response(message=err, status=400, data={})
                batch_specs.append(spec)
                total_batch_tasks += count
            if batch_specs and total_batch_tasks != new_total_tasks:
                return return_Response(
                    message=(
                        f"Sum of batch no_of_task ({total_batch_tasks}) must equal "
                        f"total_no_of_tasks ({new_total_tasks})."
                    ),
                    status=400, data={},
                )
            replace_batches = batch_specs

        if "attachment_ids" in jdata or uploaded_files:
            provided_links = _parse_attachment_links(jdata.get("attachment_ids"))
            uploaded_links = _upload_attachments_to_s3(uploaded_files) if uploaded_files else []
            append = bool(_coerce_int(jdata.get("append_attachments"), 0))
            base_links = (
                _parse_attachment_links(budget.attachment_ids) if append else []
            )
            vals["attachment_ids"] = _serialize_attachment_links(
                base_links + provided_links + uploaded_links,
            ) or False

        try:
            for sub_id, new_cost in sub_catalog_updates:
                request.env[SUBSCRIPTION_MODEL].sudo().browse(sub_id).write({"cost": new_cost})
        except (UserError, ValidationError) as e:
            request.env.cr.rollback()
            return return_Response(message=str(e), status=400, data={})
        except Exception:
            request.env.cr.rollback()
            _logger.exception("Subscription catalog update failed")
            return return_Response(
                message="Failed to update subscription catalog.",
                status=500, data={},
            )

        try:
            if vals:
                budget.write(vals)
            if recompute:
                per_task_costs = [ln.per_task_cost for ln in budget.model_line_ids]
                infra_costs = [ln.budget_amount for ln in budget.infra_line_ids]
                sub_totals = [ln.final_amount for ln in budget.subscription_line_ids]
                new_amount = _compute_budget_amount(
                    budget.total_tasks, per_task_costs, infra_costs, sub_totals,
                )
                budget.write({"budget_amount": new_amount})
            if replace_batches is not None:
                budget.batch_budget_ids.unlink()
                for spec in replace_batches:
                    spec["project_budget_id"] = budget.id
                    try:
                        with request.env.cr.savepoint():
                            request.env[BATCH_MODEL].sudo().create(spec)
                    except Exception:
                        _logger.exception(
                            "Batch creation failed for spec %s on budget %s",
                            spec, budget.id,
                        )
        except (UserError, ValidationError) as e:
            request.env.cr.rollback()
            return return_Response(message=str(e), status=400, data={})
        except Exception:
            request.env.cr.rollback()
            _logger.exception("Project budget update failed")
            return return_Response(
                message="Failed to update project budget.",
                status=500, data={},
            )

        return return_Response(
            message="Project budget updated.", status=200,
            data={"data": _budget_to_dict(budget)},
        )

    @http.route(
        "/api/v1/etp_projects/budget/requests/list",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_budget_requests(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        Request = request.env["etp.batch.budget.request"].sudo()
        domain = []
        project_id = _coerce_int(params.get("project_id"))
        if project_id:
            domain.append(("project_id", "=", project_id))
        batch_id = _coerce_int(params.get("batch_id"))
        if batch_id:
            domain.append(("batch_id", "=", batch_id))
        project_budget_id = _coerce_int(params.get("project_budget_id"))
        if project_budget_id:
            domain.append(("project_budget_id", "=", project_budget_id))
        state = (params.get("state") or "").strip()
        if state:
            valid_states = [v for v, _l in Request._fields["state"].selection]
            if state not in valid_states:
                return return_Response(
                    message=f"state must be one of {valid_states}.",
                    status=400, data={},
                )
            domain.append(("state", "=", state))
        search = (params.get("search") or "").strip()
        if search:
            domain += [
                "|", ("name", "ilike", search),
                ("justification", "ilike", search),
            ]
        total = Request.search_count(domain)
        records = Request.search(
            domain, limit=limit, offset=offset,
            order="request_date desc, id desc",
        )
        items = [_request_to_summary(r) for r in records]
        return return_Response(
            message="Budget requests fetched.", status=200,
            data={"total": total, "limit": limit, "offset": offset, "requests": items},
        )

    @http.route(
        "/api/v1/etp_projects/budget/requests/detail",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def budget_request_detail(self, **params):
        req_id = _coerce_int(params.get("id"))
        if not req_id:
            return return_Response(
                message="id is required.", status=400, data={},
            )
        rec = request.env["etp.batch.budget.request"].sudo().browse(req_id).exists()
        if not rec:
            return return_Response(
                message="Budget request not found.", status=404, data={},
            )
        return return_Response(
            message="Budget request detail fetched.", status=200,
            data={"data": _request_to_detail(rec)},
        )
