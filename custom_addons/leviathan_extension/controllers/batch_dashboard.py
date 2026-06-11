from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .main import (
    _coerce_int,
    _job_scope_domain,
    _require_leviathan_user,
)
from .task_view import _domain_from_url

CHILD_TRAJECTORY_LIMIT = 200

LIST_DEFAULT_LIMIT = 25
LIST_MAX_LIMIT = 200


def _iso_date(dt):
    # Dates go out as ISO (YYYY-MM-DD); the frontend formats for display.
    return dt.date().isoformat() if dt else ""


def _money(value):
    return round(float(value or 0.0), 2)


def _batch_scope_domain(job_scope_domain):
    """Translate a leviathan.job user-scope domain (on `user_id`) into the
    equivalent leviathan.batch.delivery domain (on the m2m `job_ids.user_id`).
    An empty job scope (the CTO/TPM "full" role) yields an empty batch scope.
    """
    out = []
    for leaf in job_scope_domain:
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
            "|", "|", "|",
            ("name", "ilike", search),
            ("job_ids.name", "ilike", search),
            ("job_ids.url", "ilike", search),
            ("job_ids.site_name", "ilike", search),
        ]
    added_by = (params.get("added_by") or "").strip()
    if added_by and added_by != "all":
        if added_by.isdigit():
            domain.append(("user_id", "=", int(added_by)))
        else:
            domain.append(("user_id.name", "ilike", added_by))
    state = (params.get("state") or "").strip()
    if state:
        requested = [s.strip() for s in state.split(",") if s.strip()]
        if requested:
            domain.append(("state", "in", requested))
    return domain


def _serialize_trajectory(job):
    category = job.category_id.name or ""
    label = job.site_name or _domain_from_url(job.url) or job.name or ""
    if category and label:
        repo_model = f"{category} × {label}"
    else:
        repo_model = category or label
    return {
        "id": job.name or "",
        "repo_model": repo_model,
        "cost": _money(job.llm_qc_cost_usd),
        "status": job.qc_verdict or job.state or "",
    }


def _serialize_batch(batch):
    trajectories = [
        _serialize_trajectory(job)
        for job in batch.job_ids[:CHILD_TRAJECTORY_LIMIT]
    ]
    return {
        "id": batch.name or "",
        "trajectory_count": batch.job_count,
        "added_by": batch.user_id.name or "",
        "submitted_on": _iso_date(batch.create_date),
        "state": batch.state,
        "trajectories": trajectories,
    }


def _added_by_options(env, batch_scope):
    owners = env["leviathan.batch.delivery"].sudo().search(
        batch_scope
    ).mapped("user_id")
    options = [{"id": "all", "label": "All members"}]
    for user in owners:
        options.append({"id": str(user.id), "label": user.name or ""})
    return options


def _columns():
    return [
        {"key": "id", "label": "Batch ID", "type": "string", "flex": 8,
         "is_row_key": True},
        {"key": "trajectory_count", "label": "# Jobs", "type": "number",
         "flex": 3, "suffix": " jobs"},
        {"key": "added_by", "label": "Added by", "type": "string", "flex": 3},
        {"key": "submitted_on", "label": "Submitted on", "type": "date",
         "flex": 3},
    ]


def _expanded():
    return {
        "row_key": "trajectories",
        "columns": [
            {"key": "_index", "label": "#", "type": "index", "width": 40},
            {"key": "id", "label": "Job ID", "type": "code", "width": 150},
            {"key": "repo_model", "label": "Category × Site", "type": "string",
             "flex": 1},
            {"key": "cost", "label": "Cost", "type": "currency", "width": 90,
             "align": "right"},
        ],
    }


def _batch_payload(env, batch_scope, params):
    Batch = env["leviathan.batch.delivery"].sudo()

    domain = batch_scope + _build_batch_domain(params)

    page = max(1, _coerce_int(params.get("page"), 1))
    per_page = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT)
    per_page = max(1, min(per_page, LIST_MAX_LIMIT))
    offset = (page - 1) * per_page

    total = Batch.search_count(domain)
    total_pages = (total + per_page - 1) // per_page
    batches = Batch.search(
        domain, limit=per_page, offset=offset, order="create_date desc, id desc"
    )

    return {
        "columns": _columns(),
        "expanded": _expanded(),
        "filters": [
            {"key": "search", "type": "search",
             "placeholder": "Search by URL..."},
            {"key": "added_by", "label": "Added by", "type": "select",
             "options": _added_by_options(env, batch_scope)},
        ],
        "pagination": {
            "current_page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        },
        "rows": [_serialize_batch(batch) for batch in batches],
    }


class LeviathanBatchDashboardController(http.Controller):

    @http.route(
        "/api/v1/leviathan_ext/batch_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def leviathan_ext_batch_dashboard(self, **kwargs):
        env = request.env
        guard = _require_leviathan_user()
        if guard is not None:
            return guard

        params = request.params or {}
        job_scope = _job_scope_domain()
        batch_scope = _batch_scope_domain(job_scope)

        return return_Response(
            message="Batches fetched successfully",
            status=200,
            data=_batch_payload(env, batch_scope, params),
        )
