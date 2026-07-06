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
import mimetypes
import os
import threading
import time
import uuid
from datetime import date, datetime

from werkzeug.utils import secure_filename

from odoo import _, api, fields, http, models, SUPERUSER_ID
from odoo.exceptions import UserError, ValidationError
from odoo.http import request
from odoo.modules.registry import Registry

from odoo.addons.api_auth_gateway.controllers.utility import (
    generate_s3_link,
    return_Response as _upstream_return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)


def _json_safe(obj):
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        try:
            return fields.Datetime.to_string(obj)
        except Exception:
            return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            return base64.b64encode(obj).decode("ascii")
    if isinstance(obj, models.BaseModel):
        return obj.ids
    try:
        return str(obj)
    except Exception:
        return None


def return_Response(message, status=200, errors=None, data=None):
    safe_data = _json_safe(data) if data is not None else data
    return _upstream_return_Response(
        message=message,
        status=status,
        errors=errors if errors is not None else [],
        data=safe_data,
    )

BUDGET_MODEL = "etp.project.aws.budget"
BATCH_MODEL = "etp.batch.budget"
PROJECT_MODEL = "project.project"
AI_MODEL = "etp.ai.model"
INFRA_MODEL = "etp.infra.type"
SUBSCRIPTION_MODEL = "etp.subscription"
TOPUP_REASON_MODEL = "etp.batch.budget.topup.reason"
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


def _upload_files_as_url_attachments(files, res_model, res_id):
    attachment_ids = []
    IrAttachment = request.env["ir.attachment"].sudo()
    for f in files or []:
        try:
            data = f.read()
        except Exception:
            _logger.exception(
                "Could not read uploaded file %s", getattr(f, "filename", "?"),
            )
            continue
        if not data:
            continue
        filename = getattr(f, "filename", "") or "attachment.bin"
        b64 = base64.b64encode(data).decode("utf-8")
        try:
            url = generate_s3_link(b64, ATTACHMENT_PREFIX, filename)
        except Exception:
            _logger.exception("S3 upload failed for %s", filename)
            continue
        if not url:
            continue
        try:
            att = IrAttachment.create({
                "name": filename,
                "type": "url",
                "url": url,
                "res_model": res_model,
                "res_id": res_id,
                "public": True,
            })
            attachment_ids.append(att.id)
        except Exception:
            _logger.exception(
                "ir.attachment create failed for %s (url=%s)", filename, url,
            )
    return attachment_ids


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


def _sync_attachment_urls_from_ids(record):
    if not record or "attachment_urls" not in record._fields:
        return
    urls = []
    for att in record.attachment_ids:
        if att.type == "url" and att.url:
            urls.append(att.url)
    record.sudo().with_context(
        tracking_disable=True, mail_notrack=True,
    ).write({"attachment_urls": _serialize_attachment_links(urls) or False})


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


def _defer_background(env, func):
    """Run ``func(new_env)`` in a daemon thread after the current tx commits.

    If the outer transaction rolls back the deferred job never runs.
    Exceptions inside the job are logged, never propagated.
    """
    dbname = env.cr.dbname
    uid = env.uid
    ctx = dict(env.context)

    def _launch():
        def _run():
            try:
                registry = Registry(dbname)
                with registry.cursor() as new_cr:
                    new_env = api.Environment(new_cr, uid, ctx)
                    try:
                        func(new_env)
                        new_cr.commit()
                    except Exception:
                        new_cr.rollback()
                        _logger.exception("Deferred background job failed")
            except Exception:
                _logger.exception("Deferred background job setup failed")

        threading.Thread(target=_run, daemon=True).start()

    env.cr.postcommit.add(_launch)


def _generate_s3_url_env(env, img_data_b64, prefix, filename):
    """S3 upload safe from a background thread. Mirrors ``generate_s3_link``."""
    connector = env["s3.connector"].sudo().search([], limit=1)
    if not connector:
        return ""
    ts = time.time_ns()
    unique_id = uuid.uuid4().hex[:12]
    if filename:
        _, ext = os.path.splitext(filename)
        if ext:
            extension = ext
        else:
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            extension = mimetypes.guess_extension(mime_type) or ".bin"
    else:
        extension = ".jpeg"
    safe_name = secure_filename(f"{ts}_{unique_id}_{prefix}")
    images_name = f"{safe_name}{extension}"
    wizard = env["s3.upload.wizard"].sudo().create({
        "s3_connector_id": connector.id,
        "upload_file": img_data_b64,
        "prefix": prefix,
        "file_name": images_name,
    })
    if wizard:
        return wizard.upload_images_in_s3_get_url() or ""
    return ""


REQUEST_MODEL = "etp.batch.budget.request"


def _request_state_label(value):
    selection = request.env[REQUEST_MODEL]._fields["state"].selection
    return dict(selection).get(value, value)


def _role_brief(role):
    if not role:
        return None
    return {
        "id": role.id,
        "name": role.name or "",
        "user_type": role.user_type or "",
    }


def _user_brief(user):
    if not user:
        return None
    return {
        "id": user.id,
        "name": user.name or "",
        "email": user.email or (user.partner_id.email if user.partner_id else "") or "",
        "role": _role_brief(user.user_role) if "user_role" in user._fields else None,
    }


def _author_brief(partner):
    if not partner:
        return None
    user = partner.user_ids[:1]
    role = user.user_role if user and "user_role" in user._fields else False
    return {
        "id": partner.id,
        "name": partner.name or "",
        "email": partner.email or "",
        "role": _role_brief(role) if role else None,
    }


def _topup_reason_brief(r):
    if not r:
        return None
    return {
        "id": r.id,
        "name": r.name or "",
        "code": r.code or "",
        "description": r.description or "",
        "sequence": r.sequence or 0,
        "active": bool(r.active),
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


def _tracking_value_brief(tv):
    try:
        def _pick(prefix):
            for suffix in ("char", "text", "integer", "float", "monetary", "datetime"):
                fname = f"{prefix}_{suffix}"
                if fname in tv._fields:
                    val = tv[fname]
                    if val:
                        return val
            return None
        field_name = ""
        field_label = ""
        if tv.field_id:
            field_name = tv.field_id.name or ""
            field_label = tv.field_id.field_description or ""
        elif "field_info" in tv._fields and tv.field_info:
            field_name = tv.field_info.get("name", "") or ""
            field_label = tv.field_info.get("desc", "") or ""
        return {
            "field": field_name,
            "field_label": field_label,
            "old_value": _pick("old_value"),
            "new_value": _pick("new_value"),
        }
    except Exception:
        _logger.exception("Failed to serialize tracking value id=%s", getattr(tv, "id", None))
        return None


def _change_log(record, limit=200):
    try:
        msgs = record.message_ids.sorted(
            key=lambda m: m.date or m.create_date, reverse=True,
        )[:limit]
    except Exception:
        _logger.exception("Failed to read message_ids for %s(%s)", record._name, record.id)
        return []
    log = []
    for m in msgs:
        try:
            log.append({
                "id": m.id,
                "date": fields.Datetime.to_string(m.date) if m.date else None,
                "author": _author_brief(m.author_id),
                "subject": str(m.subject) if m.subject else "",
                "body": str(m.body) if m.body else "",
                "message_type": m.message_type or "",
                "subtype": (str(m.subtype_id.name) if m.subtype_id and m.subtype_id.name else ""),
                "tracking_values": [
                    tvb for tvb in (
                        _tracking_value_brief(tv) for tv in m.tracking_value_ids
                    ) if tvb is not None
                ],
            })
        except Exception:
            _logger.exception("Failed to serialize message id=%s", getattr(m, "id", None))
    return log


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
        "attachment_urls": _parse_attachment_links(batch.attachment_urls),
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


def _request_type_label(value):
    if "request_type" not in request.env[REQUEST_MODEL]._fields:
        return value
    selection = request.env[REQUEST_MODEL]._fields["request_type"].selection
    return dict(selection).get(value, value)


def _with_date(user_dict, date_value):
    if user_dict is None:
        return None
    return {
        **user_dict,
        "date": (
            fields.Datetime.to_string(date_value) if date_value else None
        ),
    }


def _request_to_summary(req):
    req.ensure_one()
    has_request_type = "request_type" in req._fields
    cto_reviewer = getattr(req, "cto_reviewer_id", False)
    cto_review_date = getattr(req, "cto_review_date", False)
    cfo_approver = getattr(req, "cfo_approver_id", False)
    cfo_approval_date = getattr(req, "cfo_approval_date", False)
    return {
        "id": req.id,
        "name": req.name,
        "state": req.state,
        "state_label": _request_state_label(req.state),
        "request_type": req.request_type if has_request_type else None,
        "request_type_label": (
            _request_type_label(req.request_type) if has_request_type else None
        ),
        "revision_no": getattr(req, "revision_no", 0) or 0,
        "request_date": fields.Datetime.to_string(req.request_date) if req.request_date else None,
        "priority": req.priority,
        "requester": _with_date(_user_brief(req.requester_id), req.request_date),
        "cto_reviewer": _with_date(_user_brief(cto_reviewer), cto_review_date),
        "cto_review_date": (
            fields.Datetime.to_string(cto_review_date) if cto_review_date else None
        ),
        "cfo_approver": _with_date(_user_brief(cfo_approver), cfo_approval_date),
        "cfo_approval_date": (
            fields.Datetime.to_string(cfo_approval_date) if cfo_approval_date else None
        ),
        "approver": _with_date(_user_brief(req.approver_id), req.approval_date),
        "approval_date": fields.Datetime.to_string(req.approval_date) if req.approval_date else None,
        "batch": {
            "id": req.batch_id.id,
            "name": req.batch_id.name,
            "state": req.batch_id.state,
        } if req.batch_id else None,
        "project": {
            "id": req.project_id.id,
            "name": req.project_id.display_name,
        } if req.project_id else None,
        "project_budget": {
            "id": req.project_budget_id.id,
            "name": req.project_budget_id.name or "",
        } if req.project_budget_id else None,
        "project_budget_id": req.project_budget_id.id if req.project_budget_id else None,
        "total_tasks": req.total_tasks,
        "requested_total": req.requested_total,
        "approved_total": req.approved_total if req.state in ['approved', 'partially_approved'] else 0.0,
        "remaining_amount": req.remaining_amount,
        "sequence_number": req.sequence_number,
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
    detail = _request_to_summary(req)
    attachments = _parse_attachment_links(req.attachment_urls)
    detail.update({
        "justification": req.justification or "",
        "subject": req.subject or "",
        "message": req.message or "",
        "attachment_ids": req.attachment_ids.ids,
        "attachments": [_attachment_brief(a) for a in req.attachment_ids] if not attachments else attachments,
        "attachment_urls": attachments,
        "topup_reason_id": req.topup_reason_id.id if req.topup_reason_id else None,
        "topup_reason": _topup_reason_brief(req.topup_reason_id) if req.topup_reason_id else None,
        "rejection_reason": req.rejection_reason or "",
        'rejected_by': req.rejected_by.name if req.rejected_by else '',
        'rejected_by_role': req.rejected_by.user_role.name if req.rejected_by and req.rejected_by.user_role else '',
        "cto_review_note": getattr(req, "cto_review_note", "") or "",
        "cfo_change_request_note": getattr(req, "cfo_change_request_note", "") or "",
        "change_log": _change_log(req),
        "buffer_pct": req.buffer_pct,
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


def _batch_state_label(value):
    return dict(request.env[BATCH_MODEL]._fields["state"].selection).get(value, value)


def _batch_health_label(value):
    return dict(request.env[BATCH_MODEL]._fields["health_status"].selection).get(value, value)


def _ai_model_brief(model):
    if not model:
        return None
    return {
        "id": model.id,
        "name": model.display_name or model.name,
        "provider": model.provider or "",
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


def _batch_model_line_full(ln):
    return {
        "id": ln.id,
        "ai_model": _ai_model_brief(ln.ai_model_id),
        "ai_model_id": ln.ai_model_id.id if ln.ai_model_id else None,
        "model_name": (
            ln.ai_model_id.display_name or ln.ai_model_id.name
            if ln.ai_model_id else ""
        ),
        "provider": ln.ai_model_id.provider if ln.ai_model_id else "",
        "cost_type": ln.cost_type,
        "per_task_cost": ln.per_task_cost or 0.0,
        "per_trajectory_cost": ln.per_trajectory_cost or 0.0,
        "iterations": ln.iterations or 0,
        "approved_amount": ln.approved_amount or 0.0,
        "consumed_amount": ln.consumed_amount or 0.0,
        "remaining_amount": ln.remaining_amount or 0.0,
    }


def _batch_infra_line_full(ln):
    return {
        "id": ln.id,
        "infra_type": _infra_type_brief(ln.infra_type_id),
        "infra_id": ln.infra_type_id.id if ln.infra_type_id else None,
        "infra_name": (
            ln.infra_type_id.display_name or ln.infra_type_id.name
            if ln.infra_type_id else ""
        ),
        "provider": getattr(ln.infra_type_id, "code", "") or "" if ln.infra_type_id else "",
        "description": ln.description or "",
        "budget_amount": ln.budget_amount or 0.0,
        "per_day_cost": ln.per_day_cost or 0.0,
        "start_date": ln.start_date.isoformat() if ln.start_date else None,
        "end_date": ln.end_date.isoformat() if ln.end_date else None,
        "approved_amount": ln.approved_amount or 0.0,
        "consumed_amount": ln.consumed_amount or 0.0,
        "remaining_amount": ln.remaining_amount or 0.0,
    }


def _batch_subscription_line_full(ln):
    return {
        "id": ln.id,
        "subscription": _subscription_catalog_brief(ln.subscription_id),
        "subscription_id": ln.subscription_id.id if ln.subscription_id else None,
        "subscription_name": (
            ln.subscription_id.display_name or ln.subscription_id.name
            if ln.subscription_id else ""
        ),
        "provider": ln.subscription_id.name if ln.subscription_id else "",
        "cost_per_subscription": ln.cost_per_subscription or 0.0,
        "assigned_users": [_user_brief(u) for u in ln.assigned_user_ids],
        "assigned_to": ln.assigned_user_ids.ids,
        "subscription_count": ln.subscription_count or 0,
        "monthly_total": ln.final_amount or 0.0,
        "per_day_cost": ln.per_day_cost or 0.0,
        "start_date": ln.start_date.isoformat() if ln.start_date else None,
        "end_date": ln.end_date.isoformat() if ln.end_date else None,
        "approved_amount": ln.approved_amount or 0.0,
        "consumed_amount": ln.consumed_amount or 0.0,
        "remaining_amount": ln.remaining_amount or 0.0,
    }


def _batch_daily_task_brief(dt):
    return {
        "id": dt.id,
        "entry_date": dt.entry_date.isoformat() if dt.entry_date else None,
        "done_count": dt.done_count or 0,
        "no_of_trajectory": dt.no_of_trajectory or 0,
        "per_task_cost": dt.per_task_cost or 0.0,
        "per_trajectory_cost": dt.per_trajectory_cost or 0.0,
        "total_cost": dt.total_cost or 0.0,
        "infra_cost": dt.infra_cost or 0.0,
        "subscription_cost": dt.subscription_cost or 0.0,
        "health_status": dt.health_status or "unknown",
        "note": dt.note or "",
    }


def _batch_feedback_brief(fb):
    return {
        "id": fb.id,
        "author": _user_brief(fb.author_id),
        "date": fb.date.isoformat() if fb.date else None,
        "rating": fb.rating or "",
        "note": fb.note or "",
    }


def _batch_connected_record_brief(cr):
    return {
        "id": cr.id,
        "connected_model": cr.connected_model or "",
        "res_id": cr.res_id or 0,
        "record_display": cr.record_display or "",
    }


def _batch_info_link_brief(il):
    return {
        "id": il.id,
        "label": il.label or "",
        "url": il.url or "",
    }


def _batch_summary(batch):
    if not batch:
        return None
    pb = batch.project_budget_id
    return {
        "id": batch.id,
        "name": f"{pb.project_type}-{batch.name}",
        "state": batch.state,
        "state_label": _batch_state_label(batch.state),
        "health_status": batch.health_status or "unknown",
        "health_status_label": _batch_health_label(batch.health_status or "unknown"),
        "project": {
            "id": batch.project_id.id,
            "name": batch.project_id.display_name,
        } if batch.project_id else None,
        "project_budget": {
            "id": pb.id,
            "name": pb.name or "",
        } if pb else None,
        "requester": _user_brief(batch.requester_id),
        "approver": _user_brief(batch.approver_id),
        "approval_date": (
            fields.Datetime.to_string(batch.approval_date)
            if batch.approval_date else None
        ),
        "start_date": batch.start_date.isoformat() if batch.start_date else None,
        "end_date": batch.end_date.isoformat() if batch.end_date else None,
        "total_tasks": batch.total_tasks or 0,
        "done_tasks": batch.done_tasks or 0,
        "remaining_tasks": batch.remaining_tasks or 0,
        "buffer_pct": batch.buffer_pct or 0.0,
        "estimated_cost": batch.estimated_cost or 0.0,
        "batch_budget": batch.batch_budget or 0.0,
        "approved_amount": batch.approved_amount or 0.0,
        "consumed_cost": batch.consumed_cost or 0.0,
        "remaining_cost": batch.remaining_cost or 0.0,
        "consumed_pct": batch.consumed_pct or 0.0,
        "carried_over_amount": batch.carried_over_amount or 0.0,
        "request_count": batch.request_count or 0,
        "model_line_count": len(batch.model_line_ids),
        "infra_line_count": len(batch.infra_line_ids),
        "subscription_line_count": len(batch.subscription_line_ids),
        "attachment_urls": _parse_attachment_links(batch.attachment_urls),
    }


def _batch_full_detail(batch):
    if not batch:
        return None
    pb = batch.project_budget_id
    detail = _batch_summary(batch)
    detail.update({
        "delivered_date": (
            fields.Datetime.to_string(batch.delivered_date)
            if batch.delivered_date else None
        ),
        "closed_remaining": batch.closed_remaining or 0.0,
        "rejection_reason": batch.rejection_reason or "",
        "s3_link": batch.s3_link or "",
        "alert_80_sent": bool(batch.alert_80_sent),
        "alert_100_sent": bool(batch.alert_100_sent),
        "connected_model": batch.connected_model or "",
        "active": bool(batch.active),
    })
    detail["project_budget"] = ({
        "id": pb.id,
        "name": pb.name or "",
        "cto_user": _user_brief(pb.cto_user_id) if pb.cto_user_id else None,
        "approver_users": [_user_brief(u) for u in pb.approver_user_ids],
        "budget_amount": getattr(pb, "budget_amount", 0.0) or 0.0,
        "allocatable_amount": getattr(pb, "allocatable_amount", 0.0) or 0.0,
        "batch_budget_remain": getattr(pb, "batch_budget_remain", 0.0) or 0.0,
        "total_approved_amount": getattr(pb, "total_approved_amount", 0.0) or 0.0,
    } if pb else None)
    detail["model_lines"] = [_batch_model_line_full(ln) for ln in batch.model_line_ids]
    detail["infra_lines"] = [_batch_infra_line_full(ln) for ln in batch.infra_line_ids]
    detail["subscription_lines"] = [
        _batch_subscription_line_full(ln) for ln in batch.subscription_line_ids
    ]
    model_approved = sum((ln.approved_amount or 0.0) for ln in batch.model_line_ids)
    model_consumed = sum((ln.consumed_amount or 0.0) for ln in batch.model_line_ids)
    infra_approved = sum((ln.approved_amount or 0.0) for ln in batch.infra_line_ids)
    infra_consumed = sum((ln.consumed_amount or 0.0) for ln in batch.infra_line_ids)
    sub_approved = sum((ln.approved_amount or 0.0) for ln in batch.subscription_line_ids)
    sub_consumed = sum((ln.consumed_amount or 0.0) for ln in batch.subscription_line_ids)
    sub_monthly = sum((ln.final_amount or 0.0) for ln in batch.subscription_line_ids)
    sub_per_day = sum((ln.per_day_cost or 0.0) for ln in batch.subscription_line_ids)
    detail["totals_breakdown"] = {
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
        "buffer_pct": batch.buffer_pct or 0.0,
        "buffer_amount": max(
            0.0, (batch.batch_budget or 0.0) - (batch.estimated_cost or 0.0),
        ),
    }
    detail["requests"] = [_request_to_summary(r) for r in batch.request_ids]
    detail["daily_task_log"] = [
        _batch_daily_task_brief(dt) for dt in batch.daily_task_ids
    ]
    detail["connected_records"] = [
        _batch_connected_record_brief(cr) for cr in batch.connected_record_ids
    ]
    detail["info_links"] = [_batch_info_link_brief(il) for il in batch.info_link_ids]
    detail["feedback"] = [_batch_feedback_brief(fb) for fb in batch.feedback_ids]
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
        "/api/v1/etp_projects/budget/topup_reasons",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_topup_reasons(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = []
        if not _coerce_int(params.get("include_inactive"), 0):
            domain.append(("active", "=", True))
        search = (params.get("search") or "").strip()
        if search:
            domain += [
                "|", ("name", "ilike", search), ("code", "ilike", search),
            ]
        Reason = request.env[TOPUP_REASON_MODEL].sudo()
        total = Reason.search_count(domain)
        records = Reason.search(
            domain, limit=limit, offset=offset, order="sequence, name",
        )
        items = [_topup_reason_brief(r) for r in records]
        return return_Response(
            message="Top-up reasons fetched.", status=200,
            data={
                "total": total, "limit": limit, "offset": offset,
                "topup_reasons": items,
            },
        )

    @http.route(
        "/api/v1/etp_projects/budget/default_approvers",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_default_approvers(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        user_ids = request.env[BUDGET_MODEL].sudo()._get_default_approver_user_ids()
        users = request.env["res.users"].sudo().browse(user_ids).exists()
        search = (params.get("search") or "").strip().lower()
        if search:
            users = users.filtered(
                lambda u: search in (u.name or "").lower()
                or search in (u.login or "").lower()
                or search in (u.email or "").lower()
            )
        total = len(users)
        page = users[offset:offset + limit]
        items = [
            {
                "id": u.id,
                "name": u.name or "",
                "login": u.login or "",
                "email": u.email or (u.partner_id.email if u.partner_id else "") or "",
            }
            for u in page
        ]
        return return_Response(
            message="Default project budget approvers fetched.", status=200,
            data={
                "total": total, "limit": limit, "offset": offset,
                "approvers": items,
            },
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
        pending_uploads = []
        for f in uploaded_files or []:
            try:
                data = f.read()
            except Exception:
                _logger.exception(
                    "Could not read uploaded file %s", getattr(f, "filename", "?"),
                )
                continue
            if not data:
                continue
            pending_uploads.append((
                getattr(f, "filename", "") or "attachment.bin",
                base64.b64encode(data).decode("utf-8"),
            ))
        attachment_links = _serialize_attachment_links(provided_links)

        budget_amount = _compute_budget_amount(
            total_tasks, per_task_costs, infra_costs, sub_monthly_totals,
        )
        if is_rnd:
            budget_amount = initial_budget

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

        create_env = request.env(context=dict(
            request.env.context,
            tracking_disable=True,
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
            mail_notrack=True,
        ))

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
            budget = create_env[BUDGET_MODEL].sudo().create(vals)
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
        batch_sub_clone = [
            (0, 0, {
                "subscription_id": ln.subscription_id.id,
                "assigned_user_ids": [(6, 0, ln.assigned_user_ids.ids)],
            })
            for ln in budget.subscription_line_ids
        ]

        created_batches = create_env[BATCH_MODEL].sudo()
        for spec in batch_specs:
            spec["project_budget_id"] = budget.id
            if batch_infra_clone:
                spec["infra_line_ids"] = batch_infra_clone
            if batch_sub_clone:
                spec["subscription_line_ids"] = batch_sub_clone
            if attachment_links:
                spec["attachment_urls"] = attachment_links
            try:
                with request.env.cr.savepoint():
                    created_batches |= create_env[BATCH_MODEL].sudo().create(spec)
            except Exception:
                _logger.exception(
                    "Batch creation failed for spec %s on budget %s", spec, budget.id,
                )

        created_request = None
        if created_batches and (
            is_rnd
            or budget.model_line_ids
            or budget.infra_line_ids
            or budget.subscription_line_ids
        ):
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
                ).days
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
                    "start_date": first_batch.start_date or False,
                    "end_date": first_batch.end_date or False,
                    "requested_amount": duration_days * ((ln.budget_amount or 0.0) / 30.0),
                })
                for ln in budget.infra_line_ids
            ]
            wiz_sub_cmds = [
                (0, 0, {
                    "subscription_id": ln.subscription_id.id,
                    "assigned_user_ids": [(6, 0, ln.assigned_user_ids.ids)],
                    "requested_amount": (ln.cost_per_subscription or 0.0)
                                        * len(ln.assigned_user_ids),
                })
                for ln in budget.subscription_line_ids
            ]
            buffer_factor = 1.0 + ((first_batch.buffer_pct or 0.0) / 100.0)
            sub_total = sum(cmd[2]["requested_amount"] for cmd in wiz_sub_cmds)
            infra_total = sum(cmd[2]["requested_amount"] for cmd in wiz_infra_cmds)
            model_total = sum(cmd[2]["requested_amount"] for cmd in wiz_model_cmds)
            if is_rnd:
                # requested_total = initial_budget + infra_total + sub_total
                requested_total = initial_budget
            else:
                requested_total = (model_total + infra_total) * buffer_factor + sub_total
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
                    if wiz_sub_cmds:
                        wiz_vals["subscription_line_ids"] = wiz_sub_cmds
                    submit_env = create_env(context=dict(
                        create_env.context, etp_skip_notify=True,
                    ))
                    wizard = submit_env["etp.batch.budget.request.wizard"].sudo().create(wiz_vals)
                    result = wizard.action_submit()
                    new_req_id = result.get("res_id") if isinstance(result, dict) else None
                    if new_req_id:
                        candidate = submit_env[REQUEST_MODEL].sudo().browse(new_req_id)
                        if candidate.exists():
                            created_request = candidate
                            if attachment_links:
                                try:
                                    candidate.sudo().with_context(
                                        tracking_disable=True, mail_notrack=True,
                                    ).write({"attachment_urls": attachment_links})
                                except Exception:
                                    _logger.exception(
                                        "Failed to stamp attachment_urls on request %s",
                                        candidate.id,
                                    )
            except Exception as wexc:
                _logger.warning(
                    "Initial batch budget request skipped for budget %s batch %s: %s",
                    budget.id, first_batch.id, wexc,
                )

        budget_id_for_bg = budget.id

        def _bg_creation_mail(env_bg):
            b = env_bg[BUDGET_MODEL].browse(budget_id_for_bg).exists()
            if not b:
                return
            try:
                _send_creation_mail(b)
            except Exception:
                _logger.exception(
                    "Deferred creation mail failed for budget %s", budget_id_for_bg,
                )

        _defer_background(request.env, _bg_creation_mail)

        if pending_uploads:
            budget_id_for_uploads = budget.id
            batch_ids_for_uploads = created_batches.ids if created_batches else []
            request_id_for_uploads = created_request.id if created_request else None
            uploads_payload = list(pending_uploads)

            def _bg_uploads(env_bg):
                urls = []
                for filename, b64 in uploads_payload:
                    try:
                        url = _generate_s3_url_env(
                            env_bg, b64, ATTACHMENT_PREFIX, filename,
                        )
                    except Exception:
                        _logger.exception("S3 upload failed for %s", filename)
                        continue
                    if url:
                        urls.append(url)
                if not urls:
                    return
                b = env_bg[BUDGET_MODEL].browse(budget_id_for_uploads).exists()
                if not b:
                    return
                existing = _parse_attachment_links(b.attachment_ids)
                merged = _serialize_attachment_links(existing + urls)
                try:
                    b.sudo().with_context(
                        tracking_disable=True, mail_notrack=True,
                    ).write({"attachment_ids": merged or False})
                except Exception:
                    _logger.exception(
                        "Deferred attachment write failed for budget %s",
                        budget_id_for_uploads,
                    )
                if batch_ids_for_uploads:
                    batches_bg = env_bg[BATCH_MODEL].browse(
                        batch_ids_for_uploads,
                    ).exists()
                    for batch_bg in batches_bg:
                        batch_existing = _parse_attachment_links(
                            batch_bg.attachment_urls,
                        )
                        batch_merged = _serialize_attachment_links(
                            batch_existing + urls,
                        )
                        try:
                            batch_bg.sudo().with_context(
                                tracking_disable=True, mail_notrack=True,
                            ).write({"attachment_urls": batch_merged or False})
                        except Exception:
                            _logger.exception(
                                "Deferred attachment_urls write failed for batch %s",
                                batch_bg.id,
                            )
                if request_id_for_uploads:
                    req_bg = env_bg[REQUEST_MODEL].browse(
                        request_id_for_uploads,
                    ).exists()
                    if req_bg:
                        req_existing = _parse_attachment_links(
                            req_bg.attachment_urls,
                        )
                        req_merged = _serialize_attachment_links(
                            req_existing + urls,
                        )
                        try:
                            req_bg.sudo().with_context(
                                tracking_disable=True, mail_notrack=True,
                            ).write({"attachment_urls": req_merged or False})
                        except Exception:
                            _logger.exception(
                                "Deferred attachment_urls write failed for request %s",
                                req_bg.id,
                            )

            _defer_background(request.env, _bg_uploads)

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
        request_type = (params.get("request_type") or "").strip()
        if request_type and "request_type" in Request._fields:
            valid_types = [v for v, _l in Request._fields["request_type"].selection]
            if request_type not in valid_types:
                return return_Response(
                    message=f"request_type must be one of {valid_types}.",
                    status=400, data={},
                )
            domain.append(("request_type", "=", request_type))
        cto_reviewer_id = _coerce_int(params.get("cto_reviewer_id"))
        if cto_reviewer_id and "cto_reviewer_id" in Request._fields:
            domain.append(("cto_reviewer_id", "=", cto_reviewer_id))
        cfo_approver_id = _coerce_int(params.get("cfo_approver_id"))
        if cfo_approver_id and "cfo_approver_id" in Request._fields:
            domain.append(("cfo_approver_id", "=", cfo_approver_id))
        requester_id = _coerce_int(params.get("requester_id"))
        if requester_id:
            domain.append(("requester_id", "=", requester_id))
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
        try:
            payload = _request_to_detail(rec)
        except Exception as e:
            _logger.exception(
                "budget_request_detail serialization failed for id=%s state=%s",
                rec.id, rec.state,
            )
            return return_Response(
                message=f"Failed to serialize budget request: {e}",
                status=400, data={"errors": [str(e)]},
            )
        return return_Response(
            message="Budget request detail fetched.", status=200,
            data={"data": payload},
        )

    @http.route(
        "/api/v1/etp_projects/budget/requests/create",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def create_budget_request(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        Request = request.env[REQUEST_MODEL]
        Batch = request.env["etp.batch.budget"]

        batch_id = _coerce_int(jdata.get("batch_id"))
        if not batch_id:
            return return_Response(message="batch_id is required.", status=400, data={})
        batch = Batch.sudo().browse(batch_id).exists()
        if not batch:
            return return_Response(
                message=f"Phase {batch_id} not found.", status=404, data={},
            )

        request_type = (jdata.get("request_type") or "budget").strip()
        valid_types = [v for v, _l in Request._fields["request_type"].selection]
        if request_type not in valid_types:
            return return_Response(
                message=f"request_type must be one of {valid_types}.",
                status=400, data={},
            )

        justification = (jdata.get("justification") or "").strip()
        if not justification:
            return return_Response(
                message="justification is required.", status=400, data={},
            )

        priority = (jdata.get("priority") or "normal").strip()
        if priority not in ("low", "normal", "high", "urgent"):
            return return_Response(
                message="priority must be one of low, normal, high, urgent.",
                status=400, data={},
            )

        total_tasks = _coerce_int(jdata.get("total_tasks"), 0) or 0
        buffer_pct = _coerce_float(jdata.get("buffer_pct"), 0.0)

        model_cmds, model_sub, err = _build_request_model_cmds(
            jdata.get("model_lines") or [], total_tasks,
        )
        if err:
            return return_Response(message=err, status=400, data={})
        infra_cmds, infra_sub, err = _build_request_infra_cmds(
            jdata.get("infra_lines") or [],
        )
        if err:
            return return_Response(message=err, status=400, data={})
        sub_cmds, sub_sub, err = _build_request_subscription_cmds(
            jdata.get("subscription_lines") or [],
        )
        if err:
            return return_Response(message=err, status=400, data={})

        if "requested_total" in jdata:
            requested_total = _coerce_float(jdata.get("requested_total"), 0.0)
        else:
            subtotal = model_sub + infra_sub + sub_sub
            requested_total = subtotal * (1.0 + (buffer_pct / 100.0))

        vals = {
            "batch_id": batch.id,
            "request_type": request_type,
            "justification": justification,
            "subject": jdata.get("subject") or False,
            "message": jdata.get("message") or False,
            "priority": priority,
            "total_tasks": total_tasks,
            "buffer_pct": buffer_pct,
            "requested_total": requested_total,
            "model_line_ids": model_cmds,
            "infra_line_ids": infra_cmds,
            "subscription_line_ids": sub_cmds,
        }

        raw_attachments = jdata.get("attachment_ids") or []
        if raw_attachments:
            if not isinstance(raw_attachments, (list, tuple)):
                return return_Response(
                    message="attachment_ids must be a list of integers.",
                    status=400, data={},
                )
            attachment_ids = [
                _coerce_int(x) for x in raw_attachments if _coerce_int(x)
            ]
            missing = _missing_ids("ir.attachment", attachment_ids)
            if missing:
                return return_Response(
                    message=f"Attachment(s) not found: {missing}",
                    status=400, data={},
                )
            vals["attachment_ids"] = [(6, 0, attachment_ids)]

        reason_id = _coerce_int(jdata.get("topup_reason_id"))
        if reason_id:
            if _missing_ids(TOPUP_REASON_MODEL, [reason_id]):
                return return_Response(
                    message=f"Top-up reason {reason_id} not found.",
                    status=400, data={},
                )
            vals["topup_reason_id"] = reason_id

        try:
            req = Request.create(vals)
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception("create_budget_request failed")
            return return_Response(
                message="Something went wrong.", status=400,
                data={"errors": [str(e)]},
            )

        if uploaded_files:
            new_att_ids = _upload_files_as_url_attachments(
                uploaded_files, REQUEST_MODEL, req.id,
            )
            if new_att_ids:
                req.sudo().write({
                    "attachment_ids": [(4, i, 0) for i in new_att_ids],
                })

        if raw_attachments or uploaded_files:
            _sync_attachment_urls_from_ids(req.sudo())

        submit_flag = jdata.get("submit")
        if submit_flag is None:
            submit_flag = True
        if submit_flag:
            try:
                req.action_submit_for_approval()
            except (UserError, ValidationError) as e:
                return return_Response(
                    message=str(e), status=400, data={"request_id": req.id},
                )
            except Exception as e:
                _logger.exception("submit on create failed")
                return return_Response(
                    message="Created but submit failed.", status=400,
                    data={"request_id": req.id, "errors": [str(e)]},
                )

        return return_Response(
            message="Budget request created.", status=200,
            data={"data": _request_to_detail(req.sudo())},
        )

    @http.route(
        "/api/v1/etp_projects/budget/requests/update",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def update_budget_request(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        req_id = _coerce_int(jdata.get("id"))
        if not req_id:
            return return_Response(message="id is required.", status=400, data={})
        req = request.env[REQUEST_MODEL].browse(req_id).exists()
        if not req:
            return return_Response(
                message=f"Budget request {req_id} not found.",
                status=404, data={},
            )
        if req.state not in ("draft", "changes_required"):
            return return_Response(
                message=(
                    f"Cannot edit request in state '{req.state}'. Only draft "
                    "and changes_required are editable."
                ),
                status=400, data={},
            )

        vals = {}

        if "request_type" in jdata:
            rt = (jdata.get("request_type") or "").strip()
            valid_types = [v for v, _l in req._fields["request_type"].selection]
            if rt not in valid_types:
                return return_Response(
                    message=f"request_type must be one of {valid_types}.",
                    status=400, data={},
                )
            vals["request_type"] = rt

        if "justification" in jdata:
            justification = (jdata.get("justification") or "").strip()
            if not justification:
                return return_Response(
                    message="justification cannot be empty.",
                    status=400, data={},
                )
            vals["justification"] = justification

        if "subject" in jdata:
            vals["subject"] = jdata.get("subject") or False
        if "message" in jdata:
            vals["message"] = jdata.get("message") or False

        if "priority" in jdata:
            priority = (jdata.get("priority") or "normal").strip()
            if priority not in ("low", "normal", "high", "urgent"):
                return return_Response(
                    message="priority must be one of low, normal, high, urgent.",
                    status=400, data={},
                )
            vals["priority"] = priority

        if "total_tasks" in jdata:
            vals["total_tasks"] = _coerce_int(jdata.get("total_tasks"), 0) or 0
        if "buffer_pct" in jdata:
            vals["buffer_pct"] = _coerce_float(jdata.get("buffer_pct"), 0.0)

        total_tasks_eff = vals.get("total_tasks", req.total_tasks)

        if "model_lines" in jdata:
            cmds, _sub, err = _build_request_model_cmds(
                jdata.get("model_lines") or [], total_tasks_eff, replace=True,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            vals["model_line_ids"] = cmds
        if "infra_lines" in jdata:
            cmds, _sub, err = _build_request_infra_cmds(
                jdata.get("infra_lines") or [], replace=True,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            vals["infra_line_ids"] = cmds
        if "subscription_lines" in jdata:
            cmds, _sub, err = _build_request_subscription_cmds(
                jdata.get("subscription_lines") or [], replace=True,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            vals["subscription_line_ids"] = cmds

        if "requested_total" in jdata:
            vals["requested_total"] = _coerce_float(
                jdata.get("requested_total"), 0.0,
            )

        if "attachment_ids" in jdata:
            raw = jdata.get("attachment_ids") or []
            if not isinstance(raw, (list, tuple)):
                return return_Response(
                    message="attachment_ids must be a list of integers.",
                    status=400, data={},
                )
            ids = [_coerce_int(x) for x in raw if _coerce_int(x)]
            missing = _missing_ids("ir.attachment", ids)
            if missing:
                return return_Response(
                    message=f"Attachment(s) not found: {missing}",
                    status=400, data={},
                )
            vals["attachment_ids"] = [(6, 0, ids)]

        if "topup_reason_id" in jdata:
            raw = jdata.get("topup_reason_id")
            if raw in (None, "", 0, False):
                vals["topup_reason_id"] = False
            else:
                reason_id = _coerce_int(raw)
                if not reason_id or _missing_ids(TOPUP_REASON_MODEL, [reason_id]):
                    return return_Response(
                        message=f"Top-up reason {raw} not found.",
                        status=400, data={},
                    )
                vals["topup_reason_id"] = reason_id

        try:
            if vals:
                req.write(vals)
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception("update_budget_request failed")
            return return_Response(
                message="Something went wrong.", status=400,
                data={"errors": [str(e)]},
            )

        if uploaded_files:
            new_att_ids = _upload_files_as_url_attachments(
                uploaded_files, REQUEST_MODEL, req.id,
            )
            if new_att_ids:
                req.sudo().write({
                    "attachment_ids": [(4, i, 0) for i in new_att_ids],
                })

        if "attachment_ids" in jdata or uploaded_files:
            _sync_attachment_urls_from_ids(req.sudo())

        if jdata.get("submit"):
            try:
                req.action_submit_for_approval()
            except (UserError, ValidationError) as e:
                return return_Response(
                    message=str(e), status=400, data={"request_id": req.id},
                )
            except Exception as e:
                _logger.exception("submit on update failed")
                return return_Response(
                    message="Updated but submit failed.", status=400,
                    data={"request_id": req.id, "errors": [str(e)]},
                )

        return return_Response(
            message="Budget request updated.", status=200,
            data={"data": _request_to_detail(req.sudo())},
        )

    @http.route(
        "/api/v1/etp_projects/budget/requests/approve",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def approve_budget_request(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        req_id = _coerce_int(jdata.get("id"))
        step = (jdata.get("step") or "").strip().lower()
        if not req_id:
            return return_Response(message="id is required.", status=400, data={})
        if step not in ("cto", "cfo"):
            return return_Response(
                message="step must be 'cto' or 'cfo'.", status=400, data={},
            )

        req = request.env[REQUEST_MODEL].browse(req_id).exists()
        if not req:
            return return_Response(
                message=f"Budget request {req_id} not found.",
                status=404, data={},
            )

        if step == "cto" and req.state != "cto_review":
            return return_Response(
                message=f"Cannot CTO-approve request in state '{req.state}'.",
                status=400, data={},
            )
        if step == "cfo" and req.state != "cfo_review":
            return return_Response(
                message=f"Cannot CFO-approve request in state '{req.state}'.",
                status=400, data={},
            )

        if "request_type" in jdata:
            rt = (jdata.get("request_type") or "").strip()
            if rt and rt != req.request_type:
                return return_Response(
                    message=(
                        f"request_type cannot be changed during approval "
                        f"(current='{req.request_type}'). Send back for changes "
                        "if a type change is needed."
                    ),
                    status=400, data={},
                )

        vals = {}
        if "justification" in jdata:
            justification = (jdata.get("justification") or "").strip()
            if not justification:
                return return_Response(
                    message="justification cannot be empty.",
                    status=400, data={},
                )
            vals["justification"] = justification
        if "subject" in jdata:
            vals["subject"] = jdata.get("subject") or False
        if "message" in jdata:
            vals["message"] = jdata.get("message") or False
        if "priority" in jdata:
            priority = (jdata.get("priority") or "normal").strip()
            if priority not in ("low", "normal", "high", "urgent"):
                return return_Response(
                    message="priority must be one of low, normal, high, urgent.",
                    status=400, data={},
                )
            vals["priority"] = priority
        if "total_tasks" in jdata:
            vals["total_tasks"] = _coerce_int(jdata.get("total_tasks"), 0) or 0
        if "buffer_pct" in jdata:
            vals["buffer_pct"] = _coerce_float(jdata.get("buffer_pct"), 0.0)

        total_tasks_eff = vals.get("total_tasks", req.total_tasks)

        if "model_lines" in jdata:
            cmds, _sub, err = _build_request_model_cmds(
                jdata.get("model_lines") or [], total_tasks_eff, replace=True,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            vals["model_line_ids"] = cmds
        if "infra_lines" in jdata:
            cmds, _sub, err = _build_request_infra_cmds(
                jdata.get("infra_lines") or [], replace=True,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            vals["infra_line_ids"] = cmds
        if "subscription_lines" in jdata:
            cmds, _sub, err = _build_request_subscription_cmds(
                jdata.get("subscription_lines") or [], replace=True,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            vals["subscription_line_ids"] = cmds

        if "requested_total" in jdata:
            vals["requested_total"] = _coerce_float(
                jdata.get("requested_total"), 0.0,
            )

        if "attachment_ids" in jdata:
            raw = jdata.get("attachment_ids") or []
            if not isinstance(raw, (list, tuple)):
                return return_Response(
                    message="attachment_ids must be a list of integers.",
                    status=400, data={},
                )
            ids = [_coerce_int(x) for x in raw if _coerce_int(x)]
            missing = _missing_ids("ir.attachment", ids)
            if missing:
                return return_Response(
                    message=f"Attachment(s) not found: {missing}",
                    status=400, data={},
                )
            vals["attachment_ids"] = [(6, 0, ids)]

        try:
            if vals:
                req.write(vals)
            if step == "cto":
                req.action_cto_approve()
                _apply_line_overrides(req, jdata)
                if "approved_total" in jdata:
                    req.write({
                        "approved_total": _coerce_float(
                            jdata.get("approved_total"), 0.0,
                        ),
                    })
                if "approved_amount" in jdata:
                    req.write({
                        "approved_total": _coerce_float(
                            jdata.get("approved_amount"), 0.0,
                        ),
                    })
            else:
                _apply_line_overrides(req, jdata)
                if "approved_total" in jdata:
                    req.write({
                        "approved_total": _coerce_float(
                            jdata.get("approved_total"), 0.0,
                        ),
                    })
                if "approved_amount" in jdata:
                    req.write({
                        "approved_total": _coerce_float(
                            jdata.get("approved_amount"), 0.0,
                        ),
                    })
                req.action_cfo_approve()
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception("approve_budget_request failed")
            return return_Response(
                message="Something went wrong.", status=400,
                data={"errors": [str(e)]},
            )

        if uploaded_files:
            new_att_ids = _upload_files_as_url_attachments(
                uploaded_files, REQUEST_MODEL, req.id,
            )
            if new_att_ids:
                req.sudo().write({
                    "attachment_ids": [(4, i, 0) for i in new_att_ids],
                })

        if "attachment_ids" in jdata or uploaded_files:
            _sync_attachment_urls_from_ids(req.sudo())

        return return_Response(
            message=f"Budget request {step.upper()}-approved.", status=200,
            data={"data": _request_to_detail(req.sudo())},
        )

    @http.route(
        "/api/v1/etp_projects/budget/requests/reject",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def reject_budget_request(self, **params):
        jdata, _files = _read_multipart_or_json()
        req_id = _coerce_int(jdata.get("id"))
        step = (jdata.get("step") or "").strip().lower()
        note = (jdata.get("note") or "").strip()
        if not req_id:
            return return_Response(message="id is required.", status=400, data={})
        if step not in ("cto", "cfo"):
            return return_Response(
                message="step must be 'cto' or 'cfo'.", status=400, data={},
            )
        if not note:
            return return_Response(message="note is required.", status=400, data={})

        req = request.env[REQUEST_MODEL].browse(req_id).exists()
        if not req:
            return return_Response(
                message=f"Budget request {req_id} not found.",
                status=404, data={},
            )

        try:
            if step == "cto":
                if req.state != "cto_review":
                    return return_Response(
                        message=f"Cannot CTO-reject in state '{req.state}'.",
                        status=400, data={},
                    )
                req._do_cto_reject(note)
            else:
                if req.state != "cfo_review":
                    return return_Response(
                        message=(
                            f"Cannot CFO-request-changes in state '{req.state}'."
                        ),
                        status=400, data={},
                    )
                req._do_cfo_request_changes(note)
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception("reject_budget_request failed")
            return return_Response(
                message="Something went wrong.", status=400,
                data={"errors": [str(e)]},
            )

        verb = "CTO send-back" if step == "cto" else "CFO change request"
        return return_Response(
            message=f"Budget request returned with {verb}.", status=200,
            data={"data": _request_to_detail(req.sudo())},
        )

    @http.route(
        "/api/v1/etp_projects/budget/batches/list",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_budget_batches(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        Batch = request.env[BATCH_MODEL].sudo()
        domain = []
        project_id = _coerce_int(params.get("project_id"))
        if project_id:
            domain.append(("project_id", "=", project_id))
        project_budget_id = _coerce_int(params.get("project_budget_id"))
        if project_budget_id:
            domain.append(("project_budget_id", "=", project_budget_id))
        state = (params.get("state") or "").strip()
        if state:
            valid_states = [v for v, _l in Batch._fields["state"].selection]
            if state not in valid_states:
                return return_Response(
                    message=f"state must be one of {valid_states}.",
                    status=400, data={},
                )
            domain.append(("state", "=", state))

        if params.get('active_batch') in [1,'1']:
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
                v for v, _l in Batch._fields["health_status"].selection
            ]
            if health_status not in valid_health:
                return return_Response(
                    message=f"health_status must be one of {valid_health}.",
                    status=400, data={},
                )
            all_records = Batch.search(
                domain, order="create_date desc, id desc",
            ).filtered(
                lambda r: (r.health_status or "unknown") == health_status
            )
            total = len(all_records)
            records = all_records[offset:offset + limit]
        else:
            total = Batch.search_count(domain)
            records = Batch.search(
                domain, limit=limit, offset=offset,
                order="create_date desc, id desc",
            )
        items = [_batch_summary(b) for b in records]
        return return_Response(
            message="Phase budgets fetched.", status=200,
            data={
                "total": total, "limit": limit, "offset": offset,
                "batches": items,
            },
        )

    @http.route(
        "/api/v1/etp_projects/budget/batches/detail",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def budget_batch_detail(self, **params):
        batch_id = _coerce_int(params.get("id"))
        if not batch_id:
            return return_Response(
                message="id is required.", status=400, data={},
            )
        batch = request.env[BATCH_MODEL].sudo().browse(batch_id).exists()
        if not batch:
            return return_Response(
                message="Phase budget not found.", status=404, data={},
            )
        try:
            payload = _batch_full_detail(batch)
        except Exception as e:
            _logger.exception(
                "budget_batch_detail serialization failed for id=%s state=%s",
                batch.id, batch.state,
            )
            return return_Response(
                message=f"Failed to serialize phase budget: {e}",
                status=400, data={"errors": [str(e)]},
            )
        return return_Response(
            message="Phase budget detail fetched.", status=200,
            data={"data": payload},
        )

    @http.route(
        "/api/v1/etp_projects/budget/batches/attachments/upload",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def upload_batch_budget_attachments(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        batch_id = _coerce_int(jdata.get("id") or jdata.get("batch_id"))
        if not batch_id:
            return return_Response(
                message="id (batch_id) is required.", status=400, data={},
            )
        batch = request.env[BATCH_MODEL].sudo().browse(batch_id).exists()
        if not batch:
            return return_Response(
                message=f"Phase budget {batch_id} not found.",
                status=404, data={},
            )

        raw_ids = jdata.get("attachment_ids") or []
        if raw_ids and not isinstance(raw_ids, (list, tuple)):
            return return_Response(
                message="attachment_ids must be a list of integers.",
                status=400, data={},
            )
        existing_ids = [_coerce_int(x) for x in raw_ids if _coerce_int(x)]
        if existing_ids:
            missing = _missing_ids("ir.attachment", existing_ids)
            if missing:
                return return_Response(
                    message=f"Attachment(s) not found: {missing}",
                    status=400, data={},
                )

        new_att_ids = _upload_files_as_url_attachments(
            uploaded_files, BATCH_MODEL, batch.id,
        ) if uploaded_files else []

        replace = bool(_coerce_int(jdata.get("replace"), 0))
        if replace:
            batch.write({
                "attachment_ids": [(6, 0, existing_ids + new_att_ids)],
            })
        else:
            cmds = [(4, i, 0) for i in (existing_ids + new_att_ids)]
            if cmds:
                batch.write({"attachment_ids": cmds})

        _sync_attachment_urls_from_ids(batch)

        return return_Response(
            message="Phase budget attachments updated.", status=200,
            data={"data": {
                "id": batch.id,
                "attachment_ids": batch.attachment_ids.ids,
                "attachments": [
                    _attachment_brief(a) for a in batch.attachment_ids
                ],
            }},
        )

    @http.route(
        "/api/v1/etp_projects/budget/requests/mark_review",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def mark_review_budget_request(self, **params):
        jdata, _files = _read_multipart_or_json()
        req_id = _coerce_int(jdata.get("id"))
        step = (jdata.get("step") or "").strip().lower()
        note = (jdata.get("note") or "").strip()
        if not req_id:
            return return_Response(
                message="id is required.", status=400, data={},
            )
        if step not in ("cto", "cfo"):
            return return_Response(
                message="step must be 'cto' or 'cfo'.", status=400, data={},
            )
        req = request.env["etp.batch.budget.request"].sudo().browse(req_id).exists()
        if not req:
            return return_Response(
                message="Budget request not found.", status=404, data={},
            )
        if step == "cto" and req.state != "cto_review":
            return return_Response(
                message=(
                    f"Cannot mark CTO review in state '{req.state}'. "
                    "Request must be in cto_review."
                ),
                status=400, data={},
            )
        if step == "cfo" and req.state != "cfo_review":
            return return_Response(
                message=(
                    f"Cannot mark CFO review in state '{req.state}'. "
                    "Request must be in cfo_review."
                ),
                status=400, data={},
            )
        now = fields.Datetime.now()
        vals = {}
        if step == "cto":
            vals["cto_reviewer_id"] = request.env.user.id
            vals["cto_review_date"] = now
            if note:
                vals["cto_review_note"] = note
        else:
            vals["cfo_approver_id"] = request.env.user.id
            vals["cfo_approval_date"] = now
            if note:
                vals["cfo_change_request_note"] = note
        try:
            req.write(vals)
            body_lines = [
                f"<strong>{step.upper()} review acknowledged</strong>",
                f"Reviewer: {request.env.user.display_name}",
            ]
            if note:
                body_lines.append(f"Note: {note}")
            req.message_post(
                body="<br/>".join(body_lines),
                subject=f"{step.upper()} review acknowledged",
            )
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception("mark_review_budget_request failed")
            return return_Response(
                message="Something went wrong.", status=400,
                data={"errors": [str(e)]},
            )
        try:
            payload = _request_to_detail(req)
        except Exception as e:
            _logger.exception(
                "mark_review serialization failed for id=%s state=%s",
                req.id, req.state,
            )
            return return_Response(
                message=(
                    f"{step.upper()} review saved but response payload could "
                    f"not be built: {e}"
                ),
                status=400, data={"errors": [str(e)]},
            )
        return return_Response(
            message=f"{step.upper()} review noted.", status=200,
            data={"data": payload},
        )


def _build_request_model_cmds(entries, total_tasks, replace=False):
    cmds = []
    subtotal = 0.0
    for idx, line in enumerate(entries):
        if not isinstance(line, dict):
            return [], 0.0, f"model_lines[{idx}] must be an object."
        ai_model_id = _coerce_int(line.get("ai_model_id") or line.get("model_id"))
        if not ai_model_id:
            return [], 0.0, f"model_lines[{idx}].ai_model_id is required."
        cost_type = line.get("cost_type") or "per_task"
        if cost_type not in ("per_task", "per_trajectory"):
            return [], 0.0, (
                f"model_lines[{idx}].cost_type must be 'per_task' or "
                "'per_trajectory'."
            )
        per_task_cost = _coerce_float(line.get("per_task_cost"), 0.0)
        per_traj = _coerce_float(line.get("per_trajectory_cost"), 0.0)
        iterations = _coerce_int(
            line.get("iterations") or line.get("no_of_trajectory"), 0,
        ) or 0
        if cost_type == "per_trajectory":
            per_task_cost = per_traj * iterations
        if "requested_amount" in line:
            requested_amount = _coerce_float(line.get("requested_amount"), 0.0)
        else:
            requested_amount = (total_tasks or 0) * per_task_cost
        subtotal += requested_amount
        line_vals = {
            "ai_model_id": ai_model_id,
            "description": line.get("description") or False,
            "cost_type": cost_type,
            "per_task_cost": per_task_cost,
            "per_trajectory_cost": per_traj,
            "iterations": iterations,
            "requested_amount": requested_amount,
        }
        if "approved_amount" in line:
            line_vals["approved_amount"] = _coerce_float(
                line.get("approved_amount"), 0.0,
            )
        cmds.append((0, 0, line_vals))
    if cmds:
        missing = _missing_ids(
            "etp.ai.model", [c[2]["ai_model_id"] for c in cmds],
        )
        if missing:
            return [], 0.0, f"Model(s) not found: {missing}"
    if replace:
        return [(5, 0, 0)] + cmds, subtotal, None
    return cmds, subtotal, None


def _build_request_infra_cmds(entries, replace=False):
    cmds = []
    subtotal = 0.0
    for idx, line in enumerate(entries):
        if not isinstance(line, dict):
            return [], 0.0, f"infra_lines[{idx}] must be an object."
        infra_type_id = _coerce_int(
            line.get("infra_type_id") or line.get("infra_id"),
        )
        if not infra_type_id:
            return [], 0.0, f"infra_lines[{idx}].infra_type_id is required."
        requested_amount = _coerce_float(line.get("requested_amount"), 0.0)
        subtotal += requested_amount
        vals = {
            "infra_type_id": infra_type_id,
            "description": line.get("description") or False,
            "requested_amount": requested_amount,
        }
        sd = _coerce_date(line.get("start_date"))
        ed = _coerce_date(line.get("end_date"))
        if sd:
            vals["start_date"] = sd
        if ed:
            vals["end_date"] = ed
        if "approved_amount" in line:
            vals["approved_amount"] = _coerce_float(
                line.get("approved_amount"), 0.0,
            )
        cmds.append((0, 0, vals))
    if cmds:
        missing = _missing_ids(
            "etp.infra.type", [c[2]["infra_type_id"] for c in cmds],
        )
        if missing:
            return [], 0.0, f"Infrastructure type(s) not found: {missing}"
    if replace:
        return [(5, 0, 0)] + cmds, subtotal, None
    return cmds, subtotal, None


def _build_request_subscription_cmds(entries, replace=False):
    cmds = []
    subtotal = 0.0
    for idx, line in enumerate(entries):
        if not isinstance(line, dict):
            return [], 0.0, f"subscription_lines[{idx}] must be an object."
        subscription_id = _coerce_int(line.get("subscription_id"))
        if not subscription_id:
            return [], 0.0, (
                f"subscription_lines[{idx}].subscription_id is required."
            )
        assigned = (
            line.get("assigned_user_ids") or line.get("assigned_to") or []
        )
        if not isinstance(assigned, (list, tuple)):
            return [], 0.0, (
                f"subscription_lines[{idx}].assigned_user_ids must be a list."
            )
        assigned_ids = [_coerce_int(u) for u in assigned if _coerce_int(u)]
        requested_amount = _coerce_float(line.get("requested_amount"), 0.0)
        subtotal += requested_amount
        line_vals = {
            "subscription_id": subscription_id,
            "assigned_user_ids": [(6, 0, assigned_ids)],
            "requested_amount": requested_amount,
        }
        sd = _coerce_date(line.get("start_date"))
        ed = _coerce_date(line.get("end_date"))
        if sd:
            line_vals["start_date"] = sd
        if ed:
            line_vals["end_date"] = ed
        if "approved_amount" in line:
            line_vals["approved_amount"] = _coerce_float(
                line.get("approved_amount"), 0.0,
            )
        cmds.append((0, 0, line_vals))
    if cmds:
        missing = _missing_ids(
            "etp.subscription", [c[2]["subscription_id"] for c in cmds],
        )
        if missing:
            return [], 0.0, f"Subscription(s) not found: {missing}"
    if replace:
        return [(5, 0, 0)] + cmds, subtotal, None
    return cmds, subtotal, None


def _apply_line_overrides(req, jdata):
    for key, attr in (
        ("model_lines", "model_line_ids"),
        ("infra_lines", "infra_line_ids"),
        ("subscription_lines", "subscription_line_ids"),
    ):
        for line in jdata.get(key) or []:
            if not isinstance(line, dict):
                continue
            line_id = _coerce_int(line.get("id"))
            if not line_id or "approved_amount" not in line:
                continue
            target = getattr(req, attr).filtered(lambda l: l.id == line_id)
            if target:
                target.write({
                    "approved_amount": _coerce_float(
                        line.get("approved_amount"), 0.0,
                    ),
                })
