from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, time

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _parse_date, _user_role_tag

_logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

STAGE_SELECTION = (
    ("s1_draft", "S1 Draft"),
    ("s2_running", "S2 Running"),
    ("s2_qc", "S2 QC"),
    ("failed", "Failed"),
)
STAGE_TO_QC_STATUS = {
    "s1_draft": ("pending",),
    "s2_running": ("running",),
    "s2_qc": ("pass", "needs_revision"),
    "failed": ("fail", "error"),
}
STAGE_LABELS = dict(STAGE_SELECTION)

STATUS_SELECTION = (
    ("unstarted", "Unstarted"),
    ("generating", "Generating"),
    ("pending_review", "Pending Review"),
    ("approved", "Approved"),
    ("failed_qc", "Failed QC"),
)
STATUS_TO_QC_STATUS = {
    "unstarted": ("pending",),
    "generating": ("running",),
    "pending_review": ("needs_revision",),
    "approved": ("pass",),
    "failed_qc": ("fail", "error"),
}

COLUMNS = [
    {"key": "seq", "label": "Reference", "type": "string"},
    {"key": "raw_prompt", "label": "Raw Prompt", "type": "string"},
    {"key": "golden_prompt", "label": "Golden Prompt", "type": "string"},
    {"key": "assigned_ql_name", "label": "QL", "type": "string"},
    {"key": "category", "label": "Category", "type": "string"},
    {"key": "stage_label", "label": "Stage", "type": "string"},
    {"key": "status", "label": "Status", "type": "string"},
    {"key": "cost_usd", "label": "Cost", "type": "currency"},
    {"key": "updated_at", "label": "Updated", "type": "datetime"},
]


def _user_scope_domain(env, tag):
    if tag in ("full", "ql"):
        return []
    if tag == "tasker":
        return [("employee_ids.user_id", "=", env.user.id)]
    return [("id", "=", 0)]


def _split_csv(raw):
    if not raw:
        return []
    return [piece.strip() for piece in str(raw).split(",") if piece.strip()]


def _stage_domain(raw):
    stages = _split_csv(raw)
    statuses = set()
    for stage in stages:
        statuses.update(STAGE_TO_QC_STATUS.get(stage, ()))
    if not statuses:
        return []
    return [("qc_status", "in", list(statuses))]


def _status_domain(raw):
    statuses_in = _split_csv(raw)
    qc = set()
    for s in statuses_in:
        qc.update(STATUS_TO_QC_STATUS.get(s, ()))
    if not qc:
        return []
    return [("qc_status", "in", list(qc))]


def _resolve_category_param(env, raw):
    pieces = _split_csv(raw)
    if not pieces:
        return []
    ids = set()
    names = []
    for p in pieces:
        if p.isdigit():
            ids.add(int(p))
        else:
            names.append(p)
    if names:
        found = env["skoll.tag.life_domain"].sudo().search([("name", "in", names)])
        ids.update(found.ids)
    if not ids:
        return [("id", "=", 0)]
    return [("life_domain_ids", "in", list(ids))]


def _ql_domain(raw):
    if not raw:
        return []
    raw = str(raw).strip()
    if raw.isdigit():
        return [("employee_ids.user_id", "=", int(raw))]
    return [("employee_ids.user_id.name", "ilike", raw)]


def _search_domain(raw):
    if not raw:
        return []
    needle = str(raw).strip()
    if not needle:
        return []
    return [
        "|",
        "|",
        "|",
        ("task_id", "ilike", needle),
        ("prompt", "ilike", needle),
        ("seed_prompt", "ilike", needle),
        ("employee_ids.user_id.name", "ilike", needle),
    ]


def _date_domain(params):
    dom = []
    start = _parse_date(params.get("start_date"))
    end = _parse_date(params.get("end_date"))
    if start:
        dom.append(("create_date", ">=", datetime.combine(start, time.min)))
    if end:
        dom.append(("create_date", "<=", datetime.combine(end, time.max)))
    return dom


def _pagination(params):
    try:
        page = int(params.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        limit = int(params.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    page = max(1, page)
    limit = max(1, min(limit, MAX_LIMIT))
    offset = (page - 1) * limit
    return page, limit, offset


def _derive_stage(qc_status):
    for stage_key, statuses in STAGE_TO_QC_STATUS.items():
        if qc_status in statuses:
            return stage_key
    return "s1_draft"


def _derive_status(qc_status):
    for status_key, statuses in STATUS_TO_QC_STATUS.items():
        if qc_status in statuses:
            return status_key
    return "unstarted"


def _spec_string(task):
    parts = []
    if task.persona_id:
        parts.append(task.persona_id.name or "")
    if task.mode:
        parts.append(task.mode)
    return " · ".join([p for p in parts if p])


def _prompts(task):
    return task.seed_prompt or "", task.prompt or ""


def _serialize_task(task, cost_by_task):
    raw_prompt, golden_prompt = _prompts(task)
    assigned_user = None
    for emp in task.employee_ids:
        if emp.user_id:
            assigned_user = emp.user_id
            break
    if task.life_domain_ids:
        category_id = task.life_domain_ids[0].id
        category_name = task.life_domain_ids[0].name or ""
    else:
        category_id = 0
        category_name = ""
    stage = _derive_stage(task.qc_status)
    status = _derive_status(task.qc_status)
    return {
        "id": task.id,
        "seq": task.task_id or "",
        "spec": _spec_string(task),
        "raw_prompt": raw_prompt,
        "golden_prompt": golden_prompt,
        "is_enriched": bool(task.prompt),
        "assigned_ql_id": assigned_user.id if assigned_user else 0,
        "assigned_ql_name": assigned_user.name if assigned_user else "",
        "category_slug": str(category_id) if category_id else "",
        "category": category_name,
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, stage),
        "status": status,
        "state": task.qc_status or "",
        "review_state": task.qc_status or "",
        "cost_usd": round(cost_by_task.get(task.id, 0.0), 4),
        "attempts_used": 0,
        "attempts_remaining": 0,
        "updated_at": fields.Datetime.to_string(task.write_date) if task.write_date else None,
        "created_at": fields.Datetime.to_string(task.create_date) if task.create_date else None,
    }


class SkollTaskViewDashboardController(http.Controller):
    @http.route(
        "/api/v1/skoll_ext/task_view_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def task_view_dashboard(self, **kwargs):
        env = request.env
        tag = _user_role_tag(env)
        if tag is None:
            return return_Response(
                message="Forbidden",
                status=403,
                errors=["User has no Skoll role"],
            )
        try:
            domain = (
                _user_scope_domain(env, tag)
                + _date_domain(kwargs)
                + _resolve_category_param(env, kwargs.get("category"))
                + _ql_domain(kwargs.get("ql"))
                + _stage_domain(kwargs.get("stage"))
                + _status_domain(kwargs.get("status"))
                + _search_domain(kwargs.get("search"))
            )

            Task = env["skoll.skoll"].sudo()
            page, limit, offset = _pagination(kwargs)
            total_records = Task.search_count(domain)
            tasks = Task.search(domain, limit=limit, offset=offset, order="write_date desc, id desc")

            cost_by_task = defaultdict(float)
            if tasks:
                Generation = env["skoll.generation"].sudo()
                rows = Generation.read_group(
                    [("task_id", "in", tasks.ids)],
                    ["task_id", "total_cost:sum"],
                    ["task_id"],
                )
                for row in rows:
                    if row.get("task_id"):
                        cost_by_task[row["task_id"][0]] = float(row.get("total_cost") or 0.0)

            rows_out = [_serialize_task(task, cost_by_task) for task in tasks]
        except Exception as exc:
            _logger.exception("Skoll task_view_dashboard failed")
            return return_Response(
                message="Internal Server Error",
                status=500,
                errors=[str(exc)],
            )
        return return_Response(
            message="OK",
            status=200,
            data={
                "role": tag,
                "columns": COLUMNS,
                "rows": rows_out,
                "total_records": total_records,
                "page": page,
                "limit": limit,
            },
        )
