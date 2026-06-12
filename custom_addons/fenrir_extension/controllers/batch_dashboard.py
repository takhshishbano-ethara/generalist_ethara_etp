# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _scope, _task_cost, _user_role_tag
from .task_view_dashboard import _coerce_int

CHILD_TRAJECTORY_LIMIT = 200
LIST_DEFAULT_LIMIT = 25
LIST_MAX_LIMIT = 200


def _iso_date(dt):
    return dt.date().isoformat() if dt else ""


def _money(value):
    return round(float(value or 0.0), 2)


def _batch_scope_domain(gen_scope_domain):
    out = []
    for leaf in gen_scope_domain:
        if isinstance(leaf, (list, tuple)) and len(leaf) == 3:
            field, operator, value = leaf
            out.append((f"job_ids.{field}", operator, value))
        else:
            out.append(leaf)
    return out


def _build_batch_domain(params):
    domain = []
    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|",
            "|",
            ("name", "ilike", search),
            ("job_ids.code", "ilike", search),
            ("job_ids.title", "ilike", search),
        ]
    added_by = (params.get("added_by") or "").strip()
    if added_by and added_by != "all":
        if added_by.isdigit():
            domain.append(("user_id", "=", int(added_by)))
        else:
            domain.append(("user_id.name", "ilike", added_by))
    state = (params.get("state") or "").strip()
    if state:
        states = [s.strip() for s in state.split(",") if s.strip()]
        if states:
            domain.append(("state", "in", states))
    return domain


def _serialize_trajectory(job, category_labels, status_labels):
    category = ""
    if job.category_id:
        category = job.category_id.name or ""
    title = job.title or ""
    repo_model = " × ".join([part for part in (category, title) if part])
    return {
        "id": job.code or "",
        "repo_model": repo_model,
        "cost": _money(_task_cost(job)),
        "status": status_labels.get(job.status, "") if job.status else "",
    }


def _serialize_batch(batch, category_labels, status_labels):
    trajectories = [
        _serialize_trajectory(job, category_labels, status_labels)
        for job in batch.job_ids[:CHILD_TRAJECTORY_LIMIT]
    ]
    return {
        "id": batch.name or "",
        "trajectory_count": batch.job_count,
        "added_by": batch.user_id.name or "",
        "submitted_on": _iso_date(batch.create_date),
        "state": batch.state or "",
        "trajectories": trajectories,
    }


def _added_by_options(env, batch_scope):
    Batch = env["fenrir.batch.delivery"].sudo()
    batches = Batch.search(batch_scope)
    users = batches.mapped("user_id").sorted(key=lambda u: (u.name or "").lower())
    options = [{"id": "all", "label": "All members"}]
    for user in users:
        options.append({"id": str(user.id), "label": user.name or ""})
    return options


def _columns():
    return [
        {"key": "id", "label": "Batch ID", "type": "string", "flex": 8, "is_row_key": True},
        {"key": "trajectory_count", "label": "# Tasks", "type": "number", "flex": 3, "suffix": " tasks"},
        {"key": "added_by", "label": "Added by", "type": "string", "flex": 3},
        {"key": "submitted_on", "label": "Submitted on", "type": "date", "flex": 3},
    ]


def _expanded():
    return {
        "row_key": "trajectories",
        "columns": [
            {"key": "_index", "label": "#", "type": "number", "width": 40},
            {"key": "id", "label": "Task ID", "type": "code", "width": 150},
            {"key": "repo_model", "label": "Category × Title", "type": "string", "flex": 1},
            {"key": "cost", "label": "Cost", "type": "currency", "width": 90, "align": "right"},
        ],
    }


def _batch_payload(env, batch_scope, params):
    Batch = env["fenrir.batch.delivery"].sudo()
    Task = env["fenrir.task"].sudo()
    category_labels = {}
    status_labels = dict(Task._fields["status"].selection)
    domain = batch_scope + _build_batch_domain(params)
    page = max(1, _coerce_int(params.get("page"), 1))
    per_page = max(1, min(_coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT), LIST_MAX_LIMIT))
    offset = (page - 1) * per_page
    total = Batch.search_count(domain)
    total_pages = (total + per_page - 1) // per_page if per_page else 0
    batches = Batch.search(domain, limit=per_page, offset=offset, order="create_date desc, id desc")
    return {
        "columns": _columns(),
        "expanded": _expanded(),
        "filters": [
            {"key": "search", "type": "search", "placeholder": "Search by ID or task..."},
            {"key": "added_by", "label": "Added by", "type": "select", "options": _added_by_options(env, batch_scope)},
        ],
        "pagination": {
            "current_page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        },
        "rows": [_serialize_batch(b, category_labels, status_labels) for b in batches],
    }


class FenrirBatchDashboardController(http.Controller):
    @http.route(
        "/api/v1/fenrir_ext/batch_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def fenrir_ext_batch_dashboard(self, **kwargs):
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Fenrir batch data.",
                status=403,
            )
        params = request.params or {}
        _tag, gen_scope, _tasks = _scope(env)
        batch_scope = _batch_scope_domain(gen_scope)
        return return_Response(
            message="Batches fetched successfully",
            status=200,
            data=_batch_payload(env, batch_scope, params),
        )
