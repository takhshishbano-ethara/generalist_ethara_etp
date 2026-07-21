# -*- coding: utf-8 -*-
"""ethara_project phase ("batches") lifecycle HTTP API.

Ports the `/budget/batches/*` endpoints from etp_projects onto
`ethara.project.phase`. A "phase" is the ethara analogue of the etp
"batch budget" (`etp.batch.budget`). Field-name deltas applied throughout:

    etp.batch.budget          -> ethara.project.phase
    batch.project_budget_id    -> phase.budget_id  (FK to ethara.project.budget)
    batch.project_id           -> phase.ethara_project_id (related, via budget)
    batch.batch_budget         -> phase.phase_budget
    etp.batch.budget.info.link -> ethara.project.phase.info.link

The external request/response params `batch_id` and `project_budget_id` keep
their historical NAMES (Flutter sends and parses them unchanged) but resolve
to the ethara ids: `batch_id` -> ethara.project.phase id, `project_budget_id`
-> ethara.project.budget id.

Endpoints implemented:
- GET  /api/v1/ethara_project/budget/batches/list
- GET  /api/v1/ethara_project/budget/batches/detail
- POST /api/v1/ethara_project/budget/batches/create
- POST /api/v1/ethara_project/budget/batches/update
- POST /api/v1/ethara_project/budget/batches/deliver
- POST /api/v1/ethara_project/budget/batches/attachments/upload

Auth: every route is `type='http', auth='none', csrf=False, cors='*'` and
decorated with `@validate_token`; the caller sends the access token in the
`access-token` HTTP header. The routes must also be registered in
`data/api_endpoint_data.xml` and attached to the caller's role.
"""
import base64
import logging

from odoo import http, fields
from odoo.http import request
from odoo.exceptions import UserError, ValidationError

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .budget_api import (
    BUDGET_MODEL,
    AI_MODEL,
    INFRA_MODEL,
    SUBSCRIPTION_MODEL,
    _coerce_int,
    _coerce_float,
    _coerce_date,
    _pagination,
    _read_multipart_or_json,
    _missing_ids,
    _parse_model_entry,
    _parse_infra_entry,
    _parse_subscription_entry,
)

_logger = logging.getLogger(__name__)


PHASE_MODEL = 'ethara.project.phase'
PHASE_INFO_LINK_MODEL = 'ethara.project.phase.info.link'


# ---------------------------------------------------------------------------
# Serializer helpers (internal - not exposed via HTTP)
# ---------------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if value else None


def _dt(value):
    return fields.Datetime.to_string(value) if value else None


def _sel_label(record, field, value):
    if not value:
        return ""
    try:
        return dict(record._fields[field].selection).get(value, value) or ""
    except Exception:
        return value or ""


def _user_brief(user):
    if not user:
        return None
    return {
        "id": user.id,
        "name": user.name or "",
        "email": user.email or (
            user.partner_id.email if user.partner_id else "") or "",
    }


def _ai_model_brief(model):
    if not model:
        return None
    return {
        "id": model.id,
        "name": model.display_name or model.name,
        "provider": getattr(model, "provider", "") or "",
    }


def _infra_type_brief(infra):
    if not infra:
        return None
    return {
        "id": infra.id,
        "name": infra.display_name or infra.name,
        "code": getattr(infra, "code", "") or "",
    }


def _subscription_catalog_brief(sub):
    if not sub:
        return None
    return {
        "id": sub.id,
        "name": sub.display_name or sub.name,
        "cost": sub.cost or 0.0,
    }


def _attachment_brief(att):
    if not att:
        return None
    return {
        "id": att.id,
        "name": att.name or "",
        "mimetype": att.mimetype or "",
        "file_size": att.file_size or 0,
        "checksum": att.checksum or "",
        "url": f"/web/content/{att.id}?download=1",
    }


def _phase_attachments(phase):
    """ethara.project.phase has no attachment_ids M2M — resolve by res_id."""
    return request.env["ir.attachment"].sudo().search(
        [("res_model", "=", PHASE_MODEL), ("res_id", "=", phase.id)],
        order="id",
    )


def _model_line_full(ln):
    model = ln.ai_model_id
    return {
        "id": ln.id,
        "ai_model": _ai_model_brief(model),
        "ai_model_id": model.id if model else None,
        "ai_model_name": ln.ai_model_name or "",
        "model_name": (
            ln.ai_model_name
            or (model.display_name or model.name if model else "")
        ),
        "provider": (getattr(model, "provider", "") or "") if model else "",
        "usage_tag": (
            ln.usage_tag if "usage_tag" in ln._fields else "trajectory_building"
        ),
        "cost_type": ln.cost_type,
        "per_task_cost": ln.per_task_cost or 0.0,
        "per_trajectory_cost": ln.per_trajectory_cost or 0.0,
        "iterations": ln.iterations or 0,
        "approved_amount": ln.approved_amount or 0.0,
        "consumed_amount": (
            ln.consumed_amount if "consumed_amount" in ln._fields else 0.0
        ),
        "remaining_amount": (
            ln.remaining_amount if "remaining_amount" in ln._fields else 0.0
        ),
    }


def _infra_line_full(ln):
    infra = ln.infra_type_id
    return {
        "id": ln.id,
        "infra_type": _infra_type_brief(infra),
        "infra_id": infra.id if infra else None,
        "infra_name": (infra.display_name or infra.name) if infra else "",
        "description": ln.description or "",
        "cost": ln.budget_amount or 0.0,
        "per_day_cost": ln.per_day_cost or 0.0,
        "instance_type": ln.instance_type or "",
        "unit_price_usd": ln.unit_price_usd or 0.0,
        "price_unit": ln.price_unit or "",
        "quantity": ln.quantity or 0.0,
        "duration_hours": ln.duration_hours or 0.0,
        "ebs_storage_gb": ln.ebs_storage_gb or 0.0,
        "volume_type": ln.volume_type or "",
        "volume_rate_usd_per_gb_mo": ln.volume_rate_usd_per_gb_mo or 0.0,
        "approved_amount": ln.approved_amount or 0.0,
        "compute_amount": (
            ln.compute_amount if "compute_amount" in ln._fields else 0.0
        ),
        "storage_amount": (
            ln.storage_amount if "storage_amount" in ln._fields else 0.0
        ),
        "computed_amount": (
            ln.computed_amount if "computed_amount" in ln._fields else 0.0
        ),
        "consumed_amount": (
            ln.consumed_amount if "consumed_amount" in ln._fields else 0.0
        ),
        "remaining_amount": (
            ln.remaining_amount if "remaining_amount" in ln._fields else 0.0
        ),
    }


def _subscription_line_full(ln):
    sub = ln.subscription_id
    return {
        "id": ln.id,
        "subscription": _subscription_catalog_brief(sub),
        "subscription_id": sub.id if sub else None,
        "subscription_name": (sub.display_name or sub.name) if sub else "",
        "provider": sub.name if sub else "",
        "cost_per_subscription": ln.cost_per_subscription or 0.0,
        "assigned_users": [_user_brief(u) for u in ln.assigned_user_ids],
        "assigned_to": ln.assigned_user_ids.ids,
        "subscription_count": ln.subscription_count or 0,
        "monthly_total": ln.final_amount or 0.0,
        "per_day_cost": ln.per_day_cost or 0.0,
        "start_date": _iso(ln.start_date),
        "end_date": _iso(ln.end_date),
        "approved_amount": ln.approved_amount or 0.0,
        "consumed_amount": (
            ln.consumed_amount if "consumed_amount" in ln._fields else 0.0
        ),
        "remaining_amount": (
            ln.remaining_amount if "remaining_amount" in ln._fields else 0.0
        ),
    }


def _daily_task_brief(dt):
    return {
        "id": dt.id,
        "entry_date": _iso(dt.entry_date),
        "done_count": dt.done_count or 0,
        "no_of_trajectory": dt.no_of_trajectory or 0,
        "per_task_cost": dt.per_task_cost or 0.0,
        "per_trajectory_cost": dt.per_trajectory_cost or 0.0,
        "total_cost": dt.total_cost or 0.0,
        "infra_cost": dt.infra_cost or 0.0,
        "subscription_cost": dt.subscription_cost or 0.0,
        "note": dt.note or "",
    }


def _info_link_brief(il):
    return {
        "id": il.id,
        "label": il.label or "",
        "url": il.url or "",
    }


def _request_summary(req):
    return {
        "id": req.id,
        "name": req.name,
        "state": req.state,
        "state_label": _sel_label(req, "state", req.state),
        "request_type": (
            req.request_type if "request_type" in req._fields else None
        ),
        "revision_no": (
            req.revision_no if "revision_no" in req._fields else 0
        ) or 0,
        "request_date": _dt(req.request_date),
        "priority": req.priority,
        "requester": _user_brief(req.requester_id),
        "approver": _user_brief(req.approver_id),
        "approval_date": _dt(req.approval_date),
        "project_budget_id": req.budget_id.id if req.budget_id else None,
        "total_tasks": req.total_tasks or 0,
        "requested_total": req.requested_total or 0.0,
        "approved_total": (
            req.approved_total
            if req.state in ("approved", "partially_approved") else 0.0
        ),
        "remaining_amount": req.remaining_amount or 0.0,
        "sequence_number": req.sequence_number or 0,
    }


def _phase_to_summary(phase):
    """Lightweight row for `batches/list`.

    JSON keys mirror the etp `_batch_summary` response so existing Flutter
    models parse unchanged: the ethara `budget_id` FK is emitted as
    `project_budget_id`, `phase_budget` as `batch_budget`, and
    `ethara_project_id` as `project_id`.
    """
    pb = phase.budget_id
    project = phase.ethara_project_id
    return {
        "id": phase.id,
        "name": phase.name,
        "project_budget_id": pb.id if pb else None,
        "project_budget_name": pb.name or "" if pb else "",
        "project_id": project.id if project else None,
        "project_name": (
            project.display_name or project.name or "" if project else ""
        ),
        "state": phase.state,
        "state_label": _sel_label(phase, "state", phase.state),
        "health_status": phase.health_status or "unknown",
        "health_status_label": _sel_label(
            phase, "health_status", phase.health_status,
        ),
        "requester": _user_brief(phase.requester_id),
        "approver": _user_brief(phase.approver_id),
        "approval_date": _dt(phase.approval_date),
        "start_date": _iso(phase.start_date),
        "end_date": _iso(phase.end_date),
        "total_tasks": phase.total_tasks or 0,
        "done_tasks": phase.done_tasks or 0,
        "remaining_tasks": phase.remaining_tasks or 0,
        "buffer_pct": phase.buffer_pct or 0.0,
        "estimated_cost": phase.estimated_cost or 0.0,
        "batch_budget": phase.phase_budget or 0.0,
        "approved_amount": phase.approved_amount or 0.0,
        "consumed_cost": phase.consumed_cost or 0.0,
        "remaining_cost": phase.remaining_cost or 0.0,
        "consumed_pct": phase.consumed_pct or 0.0,
        "carried_over_amount": phase.carried_over_amount or 0.0,
        "request_count": phase.request_count or 0,
        "model_line_count": len(phase.model_line_ids),
        "infra_line_count": len(phase.infra_line_ids),
        "subscription_line_count": len(phase.subscription_line_ids),
        "attachment_urls": [
            _attachment_brief(a)["url"] for a in _phase_attachments(phase)
        ],
    }


def _phase_to_dict(phase):
    """Full detail for `batches/detail` and the create/update/deliver
    responses, mirroring the etp `_batch_full_detail` shape."""
    phase.ensure_one()
    pb = phase.budget_id
    detail = _phase_to_summary(phase)

    model_lines = [_model_line_full(ln) for ln in phase.model_line_ids]
    infra_lines = [_infra_line_full(ln) for ln in phase.infra_line_ids]
    sub_lines = [_subscription_line_full(ln) for ln in phase.subscription_line_ids]

    model_approved = sum(ln["approved_amount"] for ln in model_lines)
    model_consumed = sum(ln["consumed_amount"] for ln in model_lines)
    infra_approved = sum(ln["approved_amount"] for ln in infra_lines)
    infra_consumed = sum(ln["consumed_amount"] for ln in infra_lines)
    sub_approved = sum(ln["approved_amount"] for ln in sub_lines)
    sub_consumed = sum(ln["consumed_amount"] for ln in sub_lines)
    sub_monthly = sum(ln["monthly_total"] for ln in sub_lines)
    sub_per_day = sum(ln["per_day_cost"] for ln in sub_lines)

    attachments = _phase_attachments(phase)

    detail.update({
        "description": phase.description or "",
        "delivered_date": _dt(phase.delivered_date),
        "closed_remaining": phase.closed_remaining or 0.0,
        "rejection_reason": phase.rejection_reason or "",
        "completion_description": phase.completion_description or "",
        "est_trajectories_per_task": phase.est_trajectories_per_task or 0,
        "submitted_task_count": phase.submitted_task_count or 0,
        "delivered_per_task_cost": phase.delivered_per_task_cost or 0.0,
        "submitted_trajectories": phase.submitted_trajectories or 0,
        "submitted_batch_total": phase.submitted_batch_total or 0.0,
        "models_used": phase.models_used or "",
        "project_budget": {
            "id": pb.id,
            "name": pb.name or "",
            "budget_type": pb.project_type,
            "state": pb.state,
        } if pb else None,
        "model_lines": model_lines,
        "infra_lines": infra_lines,
        "subscription_lines": sub_lines,
        "model_approved_total": model_approved,
        "model_consumed_total": model_consumed,
        "model_remaining_total": model_approved - model_consumed,
        "infra_approved_total": infra_approved,
        "infra_consumed_total": infra_consumed,
        "infra_remaining_total": infra_approved - infra_consumed,
        "subscription_approved_total": sub_approved,
        "subscription_consumed_total": sub_consumed,
        "subscription_remaining_total": sub_approved - sub_consumed,
        "subscription_monthly_total": sub_monthly,
        "subscription_per_day_total": sub_per_day,
        "buffer_amount": max(
            0.0, (phase.phase_budget or 0.0) - (phase.estimated_cost or 0.0),
        ),
        "attachments": [_attachment_brief(a) for a in attachments],
        "attachment_ids": attachments.ids,
        "requests": [_request_summary(r) for r in phase.request_ids],
        "daily_task_log": [
            _daily_task_brief(dt) for dt in phase.daily_task_ids
        ],
        "info_links": [_info_link_brief(il) for il in phase.info_link_ids],
    })
    return detail


def _upload_binary_attachments(phase, uploaded_files):
    """Create binary ir.attachment rows bound to the phase.

    Mirrors budget_read_api.py: res_model='ethara.project.phase',
    res_id=phase.id, type='binary'. Returns the created attachment recordset.
    """
    created = request.env["ir.attachment"].sudo()
    for f in uploaded_files or []:
        try:
            data = f.read()
        except Exception:
            _logger.exception(
                "Could not read uploaded file %s",
                getattr(f, "filename", "?"),
            )
            continue
        if not data:
            continue
        att = request.env["ir.attachment"].sudo().create({
            "name": getattr(f, "filename", "") or "attachment.bin",
            "datas": base64.b64encode(data),
            "res_model": PHASE_MODEL,
            "res_id": phase.id,
            "type": "binary",
        })
        created |= att
    return created


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class EtharaBudgetPhasesController(http.Controller):
    """Phase ("batches") lifecycle endpoints for the Flutter budget flow."""

    # ---------------------------------------------------------------- list
    @http.route(
        '/api/v1/ethara_project/budget/batches/list',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def list_budget_batches(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        Phase = request.env[PHASE_MODEL].sudo()
        domain = []

        project_id = _coerce_int(params.get("project_id"))
        if project_id:
            domain.append(("ethara_project_id", "=", project_id))

        project_budget_id = _coerce_int(params.get("project_budget_id"))
        if project_budget_id:
            domain.append(("budget_id", "=", project_budget_id))

        state = (params.get("state") or "").strip()
        if state:
            valid_states = [v for v, _l in Phase._fields["state"].selection]
            if state not in valid_states:
                return return_Response(
                    message=f"state must be one of {valid_states}.",
                    status=400, data={},
                )
            domain.append(("state", "=", state))

        if params.get("active_batch") in [1, "1"]:
            domain.append(("state", "in", ["approved", "in_progress"]))

        requester_id = _coerce_int(params.get("requester_id"))
        if requester_id:
            domain.append(("requester_id", "=", requester_id))

        start_from = _coerce_date(params.get("start_date_from"))
        if start_from:
            domain.append(("start_date", ">=", start_from))
        start_to = _coerce_date(params.get("start_date_to"))
        if start_to:
            domain.append(("start_date", "<=", start_to))

        search = (params.get("search") or "").strip()
        if search:
            domain.append(("name", "ilike", search))

        health_status = (params.get("health_status") or "").strip()
        if health_status:
            valid_health = [
                v for v, _l in Phase._fields["health_status"].selection
            ]
            if health_status not in valid_health:
                return return_Response(
                    message=f"health_status must be one of {valid_health}.",
                    status=400, data={},
                )
            all_records = Phase.search(
                domain, order="create_date desc, id desc",
            ).filtered(
                lambda r: (r.health_status or "unknown") == health_status
            )
            total = len(all_records)
            records = all_records[offset:offset + limit]
        else:
            total = Phase.search_count(domain)
            records = Phase.search(
                domain, limit=limit, offset=offset,
                order="create_date desc, id desc",
            )

        items = [_phase_to_summary(p) for p in records]
        return return_Response(
            message="Phase budgets fetched.", status=200,
            data={
                "total": total, "limit": limit, "offset": offset,
                "batches": items,
            },
        )

    # -------------------------------------------------------------- detail
    @http.route(
        '/api/v1/ethara_project/budget/batches/detail',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def budget_batch_detail(self, **params):
        batch_id = _coerce_int(params.get("id") or params.get("batch_id"))
        if not batch_id:
            return return_Response(
                message="id is required.", status=400, data={},
            )
        phase = request.env[PHASE_MODEL].sudo().browse(batch_id).exists()
        if not phase:
            return return_Response(
                message="Phase budget not found.", status=404, data={},
            )
        try:
            payload = _phase_to_dict(phase)
        except Exception as e:
            _logger.exception(
                "budget_batch_detail serialization failed for id=%s state=%s",
                phase.id, phase.state,
            )
            return return_Response(
                message=f"Failed to serialize phase budget: {e}",
                status=400, data={"errors": [str(e)]},
            )
        return return_Response(
            message="Phase budget detail fetched.", status=200,
            data={"data": payload},
        )

    # -------------------------------------------------- attachments/upload
    @http.route(
        '/api/v1/ethara_project/budget/batches/attachments/upload',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def upload_batch_budget_attachments(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        batch_id = _coerce_int(jdata.get("id") or jdata.get("batch_id"))
        if not batch_id:
            return return_Response(
                message="id (batch_id) is required.", status=400, data={},
            )
        phase = request.env[PHASE_MODEL].sudo().browse(batch_id).exists()
        if not phase:
            return return_Response(
                message=f"Phase budget {batch_id} not found.",
                status=404, data={},
            )

        try:
            _upload_binary_attachments(phase, uploaded_files)
        except Exception:
            request.env.cr.rollback()
            _logger.exception(
                "Attachment upload failed for phase budget %s", phase.id,
            )
            return return_Response(
                message="Failed to upload phase budget attachments.",
                status=500, data={},
            )

        attachments = _phase_attachments(phase)
        return return_Response(
            message="Phase budget attachments updated.", status=200,
            data={"data": {
                "id": phase.id,
                "attachment_ids": attachments.ids,
                "attachments": [_attachment_brief(a) for a in attachments],
            }},
        )

    # ---------------------------------------------------------------- create
    @http.route(
        '/api/v1/ethara_project/budget/batches/create',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def create_batch_budget(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()

        project_budget_id = _coerce_int(
            jdata.get("budget_id") or jdata.get("project_budget_id"),
        )
        if not project_budget_id:
            return return_Response(
                message="budget_id (project_budget_id) is required.",
                status=400, data={},
            )
        project_budget = request.env[BUDGET_MODEL].sudo().browse(
            project_budget_id,
        ).exists()
        if not project_budget:
            return return_Response(
                message=f"Project budget {project_budget_id} not found.",
                status=404, data={},
            )

        start_date = _coerce_date(jdata.get("start_date"))
        if not start_date:
            return return_Response(
                message="start_date is required (YYYY-MM-DD).",
                status=400, data={},
            )
        end_date = _coerce_date(jdata.get("end_date"))
        if not end_date:
            return return_Response(
                message="end_date is required (YYYY-MM-DD).",
                status=400, data={},
            )
        if end_date < start_date:
            return return_Response(
                message="end_date must be on or after start_date.",
                status=400, data={},
            )

        raw_tasks = (
            jdata.get("no_of_task")
            if jdata.get("no_of_task") is not None
            else jdata.get("total_tasks")
        )
        total_tasks = _coerce_int(raw_tasks)
        if total_tasks is None or total_tasks <= 0:
            return return_Response(
                message="no_of_task must be a positive integer.",
                status=400, data={},
            )

        name = (jdata.get("name") or "").strip() or "New"
        description = jdata.get("description") or ""
        buffer_pct = _coerce_float(jdata.get("buffer_pct"), 0.0)

        raw_models = jdata.get("models") or jdata.get("model") or []
        if raw_models and not isinstance(raw_models, list):
            return return_Response(
                message="models must be a list.", status=400, data={},
            )
        model_cmds = []
        for idx, entry in enumerate(raw_models):
            line_vals, _c, err = _parse_model_entry(
                entry, idx, allow_zero_cost=True,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            model_cmds.append((0, 0, line_vals))
        if model_cmds:
            ai_ids = [c[2]["ai_model_id"] for c in model_cmds]
            missing = _missing_ids(AI_MODEL, ai_ids)
            if missing:
                return return_Response(
                    message=f"AI model ids do not exist: {missing}.",
                    status=400, data={},
                )

        raw_infra = jdata.get("infra") or jdata.get("infras") or []
        if raw_infra and not isinstance(raw_infra, list):
            return return_Response(
                message="infra must be a list.", status=400, data={},
            )
        infra_cmds = []
        for idx, entry in enumerate(raw_infra):
            line_vals, _c, err = _parse_infra_entry(entry, idx)
            if err:
                return return_Response(message=err, status=400, data={})
            infra_cmds.append((0, 0, line_vals))
        if infra_cmds:
            infra_ids = [c[2]["infra_type_id"] for c in infra_cmds]
            missing = _missing_ids(INFRA_MODEL, infra_ids)
            if missing:
                return return_Response(
                    message=f"Infra type ids do not exist: {missing}.",
                    status=400, data={},
                )

        raw_subs = jdata.get("subscription") or jdata.get("subscriptions") or []
        if raw_subs and not isinstance(raw_subs, list):
            return return_Response(
                message="subscription must be a list.", status=400, data={},
            )
        sub_cmds = []
        for idx, entry in enumerate(raw_subs):
            line_vals, _m, err = _parse_subscription_entry(entry, idx)
            if err:
                return return_Response(message=err, status=400, data={})
            sub_cmds.append((0, 0, line_vals))
        if sub_cmds:
            sub_ids = [c[2]["subscription_id"] for c in sub_cmds]
            missing = _missing_ids(SUBSCRIPTION_MODEL, sub_ids)
            if missing:
                return return_Response(
                    message=f"Subscription ids do not exist: {missing}.",
                    status=400, data={},
                )
            all_user_ids = []
            for c in sub_cmds:
                for cmd in c[2]["assigned_user_ids"]:
                    if isinstance(cmd, (list, tuple)) and len(cmd) >= 3:
                        all_user_ids.extend(cmd[2] or [])
            missing_users = _missing_ids(
                "res.users", list(set(all_user_ids)),
            )
            if missing_users:
                return return_Response(
                    message=(
                        f"Subscription assigned user ids do not exist: "
                        f"{missing_users}."
                    ),
                    status=400, data={},
                )

        create_env = request.env(context=dict(
            request.env.context,
            tracking_disable=True,
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
            mail_notrack=True,
        ))

        vals = {
            "name": name,
            "budget_id": project_budget.id,
            "start_date": start_date,
            "end_date": end_date,
            "total_tasks": total_tasks,
            "buffer_pct": buffer_pct,
            "description": description,
        }
        if model_cmds:
            vals["model_line_ids"] = model_cmds
        if infra_cmds:
            vals["infra_line_ids"] = infra_cmds
        if sub_cmds:
            vals["subscription_line_ids"] = sub_cmds

        try:
            phase = create_env[PHASE_MODEL].sudo().create(vals)
        except (UserError, ValidationError) as e:
            request.env.cr.rollback()
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            request.env.cr.rollback()
            _logger.exception("Phase budget create failed")
            return return_Response(
                message=f"Failed to create phase budget: {e}",
                status=500, data={},
            )

        if uploaded_files:
            try:
                _upload_binary_attachments(phase, uploaded_files)
            except Exception:
                _logger.exception(
                    "Attachment upload failed for phase budget %s", phase.id,
                )

        return return_Response(
            message="Phase budget created.", status=200,
            data={"data": _phase_to_dict(phase)},
        )

    # ---------------------------------------------------------------- update
    @http.route(
        '/api/v1/ethara_project/budget/batches/update',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def update_batch_budget(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        batch_id = _coerce_int(jdata.get("id") or jdata.get("batch_id"))
        if not batch_id:
            return return_Response(
                message="id is required.", status=400, data={},
            )
        phase = request.env[PHASE_MODEL].sudo().browse(batch_id).exists()
        if not phase:
            return return_Response(
                message=f"Phase budget {batch_id} not found.",
                status=404, data={},
            )

        vals = {}

        if "project_budget_id" in jdata or "budget_id" in jdata:
            new_pb = _coerce_int(
                jdata.get("project_budget_id") or jdata.get("budget_id"),
            )
            if not new_pb:
                return return_Response(
                    message="project_budget_id must be a positive integer.",
                    status=400, data={},
                )
            pb = request.env[BUDGET_MODEL].sudo().browse(new_pb).exists()
            if not pb:
                return return_Response(
                    message=f"Project budget {new_pb} not found.",
                    status=404, data={},
                )
            vals["budget_id"] = pb.id

        if "name" in jdata:
            name = (jdata.get("name") or "").strip()
            if not name:
                return return_Response(
                    message="name cannot be empty.", status=400, data={},
                )
            vals["name"] = name

        if "description" in jdata:
            vals["description"] = jdata.get("description") or ""

        if "start_date" in jdata:
            sd = _coerce_date(jdata.get("start_date"))
            if not sd:
                return return_Response(
                    message="start_date must be YYYY-MM-DD.",
                    status=400, data={},
                )
            vals["start_date"] = sd

        if "end_date" in jdata:
            ed = _coerce_date(jdata.get("end_date"))
            if not ed:
                return return_Response(
                    message="end_date must be YYYY-MM-DD.",
                    status=400, data={},
                )
            vals["end_date"] = ed

        new_start = vals.get("start_date", phase.start_date)
        new_end = vals.get("end_date", phase.end_date)
        if new_start and new_end and new_end < new_start:
            return return_Response(
                message="end_date must be on or after start_date.",
                status=400, data={},
            )

        if "no_of_task" in jdata or "total_tasks" in jdata:
            raw = (
                jdata.get("no_of_task")
                if jdata.get("no_of_task") is not None
                else jdata.get("total_tasks")
            )
            total = _coerce_int(raw)
            if total is None or total <= 0:
                return return_Response(
                    message="no_of_task must be a positive integer.",
                    status=400, data={},
                )
            vals["total_tasks"] = total

        if "buffer_pct" in jdata:
            bp = _coerce_float(jdata.get("buffer_pct"), 0.0)
            if bp < 0:
                return return_Response(
                    message="buffer_pct cannot be negative.",
                    status=400, data={},
                )
            vals["buffer_pct"] = bp

        if "active" in jdata:
            vals["active"] = bool(_coerce_int(jdata.get("active"), 0))

        if "state" in jdata:
            state = (jdata.get("state") or "").strip()
            valid_states = [
                v for v, _l in phase._fields["state"].selection
            ]
            if state not in valid_states:
                return return_Response(
                    message=f"state must be one of {valid_states}.",
                    status=400, data={},
                )
            vals["state"] = state

        if "models" in jdata or "model" in jdata:
            raw_models = jdata.get("models") or jdata.get("model") or []
            if not isinstance(raw_models, list):
                return return_Response(
                    message="models must be a list.", status=400, data={},
                )
            cmds = [(5, 0, 0)]
            for idx, entry in enumerate(raw_models):
                line_vals, _c, err = _parse_model_entry(
                    entry, idx, allow_zero_cost=True,
                )
                if err:
                    return return_Response(message=err, status=400, data={})
                cmds.append((0, 0, line_vals))
            ai_ids = [c[2]["ai_model_id"] for c in cmds if c[0] == 0]
            missing = _missing_ids(AI_MODEL, ai_ids)
            if missing:
                return return_Response(
                    message=f"AI model ids do not exist: {missing}.",
                    status=400, data={},
                )
            vals["model_line_ids"] = cmds

        if "infra" in jdata or "infras" in jdata:
            raw_infra = jdata.get("infra") or jdata.get("infras") or []
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
            infra_ids = [c[2]["infra_type_id"] for c in cmds if c[0] == 0]
            missing = _missing_ids(INFRA_MODEL, infra_ids)
            if missing:
                return return_Response(
                    message=f"Infra type ids do not exist: {missing}.",
                    status=400, data={},
                )
            vals["infra_line_ids"] = cmds

        if "subscription" in jdata or "subscriptions" in jdata:
            raw_subs = (
                jdata.get("subscription") or jdata.get("subscriptions") or []
            )
            if not isinstance(raw_subs, list):
                return return_Response(
                    message="subscription must be a list.",
                    status=400, data={},
                )
            cmds = [(5, 0, 0)]
            for idx, entry in enumerate(raw_subs):
                line_vals, _m, err = _parse_subscription_entry(entry, idx)
                if err:
                    return return_Response(message=err, status=400, data={})
                cmds.append((0, 0, line_vals))
            sub_ids = [c[2]["subscription_id"] for c in cmds if c[0] == 0]
            missing = _missing_ids(SUBSCRIPTION_MODEL, sub_ids)
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
            missing_users = _missing_ids(
                "res.users", list(set(all_user_ids)),
            )
            if missing_users:
                return return_Response(
                    message=(
                        f"Subscription assigned user ids do not exist: "
                        f"{missing_users}."
                    ),
                    status=400, data={},
                )
            vals["subscription_line_ids"] = cmds

        try:
            if vals:
                phase.write(vals)
        except (UserError, ValidationError) as e:
            request.env.cr.rollback()
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            request.env.cr.rollback()
            _logger.exception(
                "Phase budget update failed for id=%s", phase.id,
            )
            return return_Response(
                message=f"Failed to update phase budget: {e}",
                status=500, data={},
            )

        if uploaded_files:
            try:
                _upload_binary_attachments(phase, uploaded_files)
            except Exception:
                _logger.exception(
                    "Attachment upload failed for phase budget %s", phase.id,
                )

        return return_Response(
            message="Phase budget updated.", status=200,
            data={"data": _phase_to_dict(phase)},
        )

    # --------------------------------------------------------------- deliver
    @http.route(
        '/api/v1/ethara_project/budget/batches/deliver',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def deliver_batch_budget(self, **params):
        jdata, _files = _read_multipart_or_json()
        batch_id = _coerce_int(jdata.get("id") or jdata.get("batch_id"))
        url = (jdata.get("url") or "").strip()
        completion_description = (
            jdata.get("completion_description") or ""
        ).strip()
        label = (jdata.get("label") or "").strip() or "Completion Link"
        submitted_task_count = _coerce_int(jdata.get("submitted_task_count"))
        delivered_per_task_cost = _coerce_float(
            jdata.get("delivered_per_task_cost")
        )
        submitted_trajectories = _coerce_int(jdata.get("submitted_trajectories"))
        models_used = (jdata.get("models_used") or "").strip()

        if not batch_id:
            return return_Response(
                message="id is required.", status=400, data={},
            )
        if not url:
            return return_Response(
                message="url is required.", status=400, data={},
            )
        if not completion_description:
            return return_Response(
                message="completion_description is required.",
                status=400, data={},
            )
        if not submitted_task_count or submitted_task_count <= 0:
            return return_Response(
                message="submitted_task_count is required and must be > 0.",
                status=400, data={},
            )
        if not delivered_per_task_cost or delivered_per_task_cost <= 0.0:
            return return_Response(
                message="delivered_per_task_cost is required and must be > 0.",
                status=400, data={},
            )
        if not submitted_trajectories or submitted_trajectories <= 0:
            return return_Response(
                message="submitted_trajectories is required and must be > 0.",
                status=400, data={},
            )
        if not models_used:
            return return_Response(
                message="models_used is required.",
                status=400, data={},
            )

        phase = request.env[PHASE_MODEL].sudo().browse(batch_id).exists()
        if not phase:
            return return_Response(
                message=f"Phase budget {batch_id} not found.",
                status=404, data={},
            )
        if phase.state not in ("approved", "in_progress"):
            return return_Response(
                message=(
                    f"Cannot deliver phase in state '{phase.state}'. "
                    "Only Approved or In Progress phases can be delivered."
                ),
                status=400, data={},
            )

        try:
            phase.write({
                "completion_description": completion_description,
                "submitted_task_count": submitted_task_count,
                "delivered_per_task_cost": delivered_per_task_cost,
                "submitted_trajectories": submitted_trajectories,
                "models_used": models_used,
            })
            request.env[PHASE_INFO_LINK_MODEL].sudo().create({
                "phase_id": phase.id,
                "label": label,
                "url": url,
            })
            phase.action_deliver()
        except (UserError, ValidationError) as exc:
            request.env.cr.rollback()
            return return_Response(
                message=str(exc), status=400, data={},
            )
        except Exception as exc:
            request.env.cr.rollback()
            _logger.exception(
                "Failed to deliver phase budget %s: %s", phase.id, exc,
            )
            return return_Response(
                message="Failed to mark phase budget as delivered.",
                status=500, data={},
            )

        return return_Response(
            message="Phase budget marked as delivered.", status=200,
            data={"data": _phase_to_dict(phase)},
        )
