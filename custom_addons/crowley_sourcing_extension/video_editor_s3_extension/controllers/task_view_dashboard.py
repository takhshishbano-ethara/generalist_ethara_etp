from datetime import datetime

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
SORT_FIELDS = {
    "created_date": "create_date",
    "seq": "name",
    "topic": "topic_name",
    "state": "state",
}

FULL_ACCESS_ROLE_XMLIDS = (
    "api_auth_gateway.role_cto_technical",
    "api_auth_gateway.role_tpm_technical",
)

PL_ROLE_XMLIDS = (
    "api_auth_gateway.role_pl_technical",
    "api_auth_gateway.role_pl_stem",
    "api_auth_gateway.role_pl_non_stem",
)

QR_ROLE_XMLIDS = (
    "api_auth_gateway.role_qc_technical",
    "api_auth_gateway.role_qc_stem",
    "api_auth_gateway.role_qc_non_stem",
)

TASKER_ROLE_XMLIDS = (
    "api_auth_gateway.role_tasker_technical",
    "api_auth_gateway.role_tasker_stem",
    "api_auth_gateway.role_tasker_non_stem",
)


def _get_role_ids(env, xmlids):
    ids = []
    for xmlid in xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids


def _user_role_tag(env):
    role = env.user.user_role
    if not role:
        return None
    role_id = role.id
    if role_id in _get_role_ids(env, FULL_ACCESS_ROLE_XMLIDS):
        return "full"
    if role_id in _get_role_ids(env, PL_ROLE_XMLIDS):
        return "pl"
    if role_id in _get_role_ids(env, QR_ROLE_XMLIDS):
        return "qr"
    if role_id in _get_role_ids(env, TASKER_ROLE_XMLIDS):
        return "tasker"
    return None


def _scope(env):
    """Role-scoped view of video.editor.project, keyed on ``assigned_to``.

    CTO/TPM see every project; PL/QC see the projects assigned to the
    taskers of the project.project teams they lead or review (and their
    own); taskers and everyone else see only their own assignments.
    """
    tag = _user_role_tag(env)
    user = env.user
    Project = env["project.project"].sudo()
    Employee = env["hr.employee"].sudo()
    if tag == "full":
        return tag, []
    employee = Employee.search([("user_id", "=", user.id)], limit=1)
    if tag in ("pl", "qr"):
        field = "project_lead" if tag == "pl" else "project_qc_reviewer"
        projects = Project.search([(field, "in", employee.ids)])
        taskers = projects.mapped("project_tasker")
        user_ids = (taskers.mapped("user_id") | user).ids
        return tag, [("assigned_to", "in", user_ids)]
    return "tasker", [("assigned_to", "=", user.id)]


def _parse_date(raw, label):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, return_Response(
            message=f"Invalid {label} '{raw}'. Expected YYYY-MM-DD.",
            status=400,
        )


def _create_date_domain(start, end):
    domain = []
    if start:
        domain.append(("create_date", ">=", datetime.combine(start, datetime.min.time())))
    if end:
        domain.append(("create_date", "<=", datetime.combine(end, datetime.max.time())))
    return domain


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


def _category_name(project):
    if project.category_id:
        return project.category_id.name or ""
    return dict(project._fields["category"].selection).get(project.category, "") or ""


def _sub_category_name(project):
    if project.sub_category_id:
        return project.sub_category_id.name or ""
    return dict(project._fields["sub_category"].selection).get(project.sub_category, "") or ""


def _iso(value):
    return value.isoformat() if value else None


def _build_task_view_domain(env, params):
    domain = []
    Project = env["video.editor.project"]

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

    for param, field in (
        ("status", "state"),
        ("qc_result", "llm_qc_result"),
        ("review_status", "review_status"),
        ("style", "style"),
    ):
        raw = (params.get(param) or "").strip()
        if raw:
            values, error = _parse_selection(Project, field, raw, param)
            if error is not None:
                return None, error
            if values:
                domain.append((field, "in", values))

    raw_category = (params.get("category") or "").strip()
    if raw_category:
        if raw_category.isdigit():
            domain.append(("category_id", "=", int(raw_category)))
        else:
            domain.append(("category_id.name", "ilike", raw_category))

    raw_sub_category = (params.get("sub_category") or "").strip()
    if raw_sub_category:
        if raw_sub_category.isdigit():
            domain.append(("sub_category_id", "=", int(raw_sub_category)))
        else:
            domain.append(("sub_category_id.name", "ilike", raw_sub_category))

    raw_tasker = (params.get("tasker") or "").strip()
    if raw_tasker:
        if raw_tasker.isdigit():
            domain.append(("assigned_to", "=", int(raw_tasker)))
        else:
            domain.append(("assigned_to.name", "ilike", raw_tasker))

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|",
            "|",
            ("name", "ilike", search),
            ("topic_name", "ilike", search),
            ("youtube_url", "ilike", search),
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


def _serialize_job(job, type_labels, status_labels):
    return {
        "id": job.id,
        "job_type": job.job_type or "",
        "job_type_label": type_labels.get(job.job_type, ""),
        "status": job.status or "",
        "status_label": status_labels.get(job.status, ""),
        "progress": job.progress_text or "",
        "started": _iso(job.started_at),
        "finished": _iso(job.finished_at),
        "duration": job.duration_ms or 0,  # milliseconds
        "error_message": job.error_message or "",
    }


def _serialize_row(project, state_labels, style_labels, qc_labels, review_labels,
                   job_type_labels, job_status_labels):
    return {
        "id": project.id,
        "seq": project.name or "",
        "category": _category_name(project),
        "sub_category": _sub_category_name(project),
        "topic": project.topic_name or "",
        "style": project.style or "",
        "style_label": style_labels.get(project.style, ""),
        "youtube_url": project.youtube_url or "",
        "start_time": project.youtube_start_time or "",
        "end_time": project.youtube_end_time or "",
        "trimmed_s3_url": project.output_s3_url or "",
        "assigned_to_id": project.assigned_to.id or 0,
        "assigned_to": project.assigned_to.name or "",
        "state": project.state or "",
        "state_label": state_labels.get(project.state, ""),
        "qc_result": project.llm_qc_result or "",
        "qc_result_label": qc_labels.get(project.llm_qc_result, ""),
        "review_status": project.review_status or "",
        "review_status_label": review_labels.get(project.review_status, ""),
        "created_date": _iso(project.create_date),
        "general_information": {
            "prompt": project.prompt or "",
        },
        "extra_information": {
            "jobs": [
                _serialize_job(job, job_type_labels, job_status_labels)
                for job in project.job_ids
            ],
        },
    }


class VideoEditorTaskViewDashboardController(http.Controller):

    @http.route(
        "/api/v1/video_editor_ext/task_view_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def video_editor_ext_task_view_dashboard(self, **kwargs):
        """Paginated, filterable, sortable task listing for video.editor.project.

        Single endpoint: each row also embeds general_information (prompt)
        and extra_information (jobs) so no separate detail call is needed.
        """
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Crowley Sourcing task view.",
                status=403,
            )

        params = request.params or {}
        domain, error = _build_task_view_domain(env, params)
        if error is not None:
            return error
        order, error = _resolve_order(params)
        if error is not None:
            return error

        tag, scope = _scope(env)
        domain = scope + domain

        page = max(1, _coerce_int(params.get("page"), 1))
        limit = min(max(1, _coerce_int(params.get("limit"), DEFAULT_LIMIT)), MAX_LIMIT)
        offset = (page - 1) * limit

        Project = env["video.editor.project"].sudo()
        total = Project.search_count(domain)
        records = Project.search(domain, limit=limit, offset=offset, order=order)
        state_labels = dict(Project._fields["state"].selection)
        style_labels = dict(Project._fields["style"].selection)
        qc_labels = dict(Project._fields["llm_qc_result"].selection)
        review_labels = dict(Project._fields["review_status"].selection)
        Job = env["video.editor.job"].sudo()
        job_type_labels = dict(Job._fields["job_type"].selection)
        job_status_labels = dict(Job._fields["status"].selection)
        rows = [
            _serialize_row(p, state_labels, style_labels, qc_labels, review_labels,
                           job_type_labels, job_status_labels)
            for p in records
        ]
        total_pages = (total + limit - 1) // limit if total else 0
        data = {
            "columns": [
                {"key": "seq", "label": "Name", "type": "string"},
                {"key": "category", "label": "Category", "type": "string"},
                {"key": "sub_category", "label": "Sub-Category", "type": "string"},
                {"key": "topic", "label": "Topic", "type": "string"},
                {"key": "style_label", "label": "Style", "type": "string"},
                {"key": "youtube_url", "label": "YouTube URL", "type": "string"},
                {"key": "trimmed_s3_url", "label": "Trimmed S3 URL", "type": "string"},
                {"key": "assigned_to", "label": "Assigned To", "type": "string"},
                {"key": "state_label", "label": "Stage", "type": "string"},
                {"key": "qc_result_label", "label": "QC", "type": "string"},
                {"key": "review_status_label", "label": "Review", "type": "string"},
                {"key": "created_date", "label": "Created", "type": "string"},
            ],
            "rows": rows,
            "pagination": {
                "total_records": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
            },
        }
        return return_Response(message="OK", status=200, data=data)
