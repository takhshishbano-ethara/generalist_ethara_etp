from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    _create_date_domain,
    _parse_date,
    _scope,
    _user_role_tag,
)

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
SORT_FIELDS = {
    "created_date": "create_date",
    "score": "score",
    "grade": "grade",
    "seq": "name",
}


def _coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_selection(model, field_name, raw, label):
    valid = dict(model._fields[field_name].selection)
    requested = [v.strip() for v in raw.split(",") if v.strip()]
    invalid = [v for v in requested if v not in valid]
    if invalid:
        return None, return_Response(
            message=f"Invalid {label} value(s): {', '.join(invalid)}.",
            status=400,
        )
    return requested, None


def _build_task_view_domain(env, params):
    domain = []
    Job = env["vegeta.job"]

    raw_start = (params.get("start_date") or "").strip()
    raw_end = (params.get("end_date") or "").strip()
    start = end = None
    if raw_start:
        start, error = _parse_date(raw_start, "start_date")
        if error is not None:
            return None, error
    if raw_end:
        end, error = _parse_date(raw_end, "end_date")
        if error is not None:
            return None, error
    if start and end and start > end:
        return None, return_Response(
            message="Invalid date range: start_date must be on or before end_date.",
            status=400,
        )
    domain += _create_date_domain(start, end)

    raw_status = (params.get("status") or "").strip()
    if raw_status:
        values, error = _parse_selection(Job, "state", raw_status, "status")
        if error is not None:
            return None, error
        if values:
            domain.append(("state", "in", values))

    raw_verdict = (params.get("qc_verdict") or "").strip()
    if raw_verdict:
        values, error = _parse_selection(
            Job, "qc_verdict", raw_verdict, "qc_verdict"
        )
        if error is not None:
            return None, error
        if values:
            domain.append(("qc_verdict", "in", values))

    raw_category = (params.get("category") or "").strip()
    if raw_category:
        if raw_category.isdigit():
            domain.append(("category_id", "=", int(raw_category)))
        else:
            domain.append(("category_id.name", "ilike", raw_category))

    raw_tasker = (params.get("tasker") or "").strip()
    if raw_tasker:
        if raw_tasker.isdigit():
            domain.append(("user_id", "=", int(raw_tasker)))
        else:
            domain.append(("user_id.name", "ilike", raw_tasker))

    raw_added_by = (params.get("added_by") or "").strip()
    if raw_added_by:
        if raw_added_by.isdigit():
            domain.append(("create_uid", "=", int(raw_added_by)))
        else:
            domain.append(("create_uid.name", "ilike", raw_added_by))

    if (params.get("url_added") or "").strip() == "1":
        domain.append(("url", "!=", False))

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|",
            "|",
            ("site_name", "ilike", search),
            ("url", "ilike", search),
            ("user_id.name", "ilike", search),
        ]

    return domain, None


def _resolve_order(params):
    raw_sort = (params.get("sort_by") or "created_date").strip()
    if raw_sort not in SORT_FIELDS:
        return None, return_Response(
            message=(
                f"Invalid sort_by '{raw_sort}'. "
                f"Allowed: {', '.join(sorted(SORT_FIELDS))}."
            ),
            status=400,
        )
    direction = "asc" if (params.get("sort_order") or "").strip().lower() == "asc" else "desc"
    return f"{SORT_FIELDS[raw_sort]} {direction}, id desc", None


def _serialize(job, verdict_labels):
    return {
        "id": job.id,
        "task_name": job.site_name or job.url or "",
        "seq": job.name or "",
        "url": job.url or "",
        "category": job.category_id.name or "",
        "score": job.score,
        "grade": job.grade or "",
        "state": job.state or "",
        "qc_verdict": job.qc_verdict or "",
        "qc_verdict_label": verdict_labels.get(job.qc_verdict, ""),
        "tasker_name": job.user_id.name or "",
        "created_date": job.create_date.isoformat() if job.create_date else None,
        "added_by": job.create_uid.name or "",
        "url_added": bool(job.url),
    }


class VegetaTaskViewDashboardController(http.Controller):

    @http.route(
        "/api/v1/vegeta_ext/task_view_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def vegeta_ext_task_view_dashboard(self, **kwargs):
        """Paginated, filterable, sortable task listing for vegeta.job."""
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Vegeta task view.",
                status=403,
            )

        params = request.params or {}
        domain, error = _build_task_view_domain(env, params)
        if error is not None:
            return error
        order, error = _resolve_order(params)
        if error is not None:
            return error

        tag, scope, projects = _scope(env)
        domain = scope + domain

        page = max(1, _coerce_int(params.get("page"), 1))
        limit = min(max(1, _coerce_int(params.get("limit"), DEFAULT_LIMIT)), MAX_LIMIT)
        offset = (page - 1) * limit

        Job = env["vegeta.job"].sudo()
        total = Job.search_count(domain)
        records = Job.search(domain, limit=limit, offset=offset, order=order)
        verdict_labels = dict(Job._fields["qc_verdict"].selection)
        tasks = [_serialize(job, verdict_labels) for job in records]
        total_pages = (total + limit - 1) // limit if total else 0
        data = {
            "columns": [{"key": "seq", "label": "Seq.", "type": "string"},
                        {"key": "task_name", "label": "Task", "type": "string"},
                        {"key": "url", "label": "URL", "type": "string"},
                        {"key": "category", "label": "Category", "type": "string"},
                        {"key": "score", "label": "Score", "type": "string"},
                        {"key": "state", "label": "Status", "type": "string"},
                        {"key": "grade", "label": "Grade", "type": "string"},
                        {"key": "qc_verdict", "label": "QC Verdict", "type": "string"},
                        {"key": "tasker_name", "label": "Tasker", "type": "string"},
                        {"key": "created_date", "label": "Created", "type": "string"}],
            "rows": tasks,
            "pagination": {
                "total_records": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
            },
        }
        return return_Response(message="OK", status=200, data=data)
