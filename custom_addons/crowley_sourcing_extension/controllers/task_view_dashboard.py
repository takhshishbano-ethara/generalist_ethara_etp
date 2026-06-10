from datetime import datetime

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _create_date_domain, _parse_date, _scope, _user_role_tag

LIST_DEFAULT_LIMIT = 25
LIST_MAX_LIMIT = 200

# Stage badge shown in the Tasks table (Draft / Processed / Done / Failed),
# derived from the raw video.editor.project state.
STAGE_TO_STATES = {
    "draft": ("draft",),
    "processed": ("processing", "exporting", "processed"),
    "done": ("exported",),
    "failed": ("error",),
}

SORT_FIELDS = {
    "reference": "name",
    "topic": "topic_name",
    "updated": "write_date",
    "cost": "llm_qc_cost_usd",
    "created_date": "create_date",
}


def _coerce_int(value, default):
    try:
        result = int(value)
        return result if result >= 0 else default
    except (TypeError, ValueError):
        return default


def _iso(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _error_response(message, status=400):
    return return_Response(message=message, status=status)


def _initials(name):
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _fmt_duration(seconds):
    total = int(round(seconds or 0))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def _category_name(project):
    if project.category_id:
        return project.category_id.name or ""
    return dict(project._fields["category"].selection).get(project.category, "") or ""


def _sub_category_name(project):
    if project.sub_category_id:
        return project.sub_category_id.name or ""
    return (
        dict(project._fields["sub_category"].selection).get(project.sub_category, "")
        or ""
    )


def _stage(state):
    if state == "draft":
        return "draft", "Draft"
    if state in ("processing", "exporting", "processed"):
        return "processed", "Processed"
    if state == "exported":
        return "done", "Done"
    if state == "error":
        return "failed", "Failed"
    return state or "", state or ""


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
        return None, _error_response(
            "Invalid date range: start_date must be on or before end_date."
        )
    domain += _create_date_domain(start, end)

    raw_stage = (params.get("stage") or "").strip()
    if raw_stage:
        if raw_stage not in STAGE_TO_STATES:
            return None, _error_response(
                f"Invalid stage '{raw_stage}'. Allowed: "
                f"{', '.join(STAGE_TO_STATES)}."
            )
        domain.append(("state", "in", list(STAGE_TO_STATES[raw_stage])))

    raw_qc = (params.get("qc") or params.get("qc_verdict") or "").strip()
    if raw_qc:
        valid = dict(Project._fields["llm_qc_result"].selection)
        verdicts = [v.strip() for v in raw_qc.split(",") if v.strip()]
        invalid = [v for v in verdicts if v not in valid]
        if invalid:
            return None, _error_response(
                f"Invalid qc value(s): {', '.join(invalid)}."
            )
        domain.append(("llm_qc_result", "in", verdicts))

    raw_category = (params.get("category") or "").strip()
    if raw_category:
        if raw_category.isdigit():
            domain.append(("category_id", "=", int(raw_category)))
        else:
            domain += [
                "|",
                ("category", "=", raw_category),
                ("category_id.code", "=", raw_category),
            ]

    raw_sub_category = (params.get("sub_category") or "").strip()
    if raw_sub_category:
        if raw_sub_category.isdigit():
            domain.append(("sub_category_id", "=", int(raw_sub_category)))
        else:
            domain += [
                "|",
                ("sub_category", "=", raw_sub_category),
                ("sub_category_id.code", "=", raw_sub_category),
            ]

    raw_assigned = (params.get("assigned") or params.get("tasker") or "").strip()
    if raw_assigned:
        if raw_assigned.isdigit():
            domain.append(("assigned_to", "=", int(raw_assigned)))
        else:
            domain.append(("assigned_to.name", "ilike", raw_assigned))

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
    raw_sort = (params.get("sort_by") or "updated").strip()
    if raw_sort not in SORT_FIELDS:
        return None, _error_response(
            f"Invalid sort_by '{raw_sort}'. Allowed: "
            f"{', '.join(sorted(SORT_FIELDS))}."
        )
    direction = (
        "asc" if (params.get("sort_order") or "").strip().lower() == "asc" else "desc"
    )
    return f"{SORT_FIELDS[raw_sort]} {direction}, id desc", None


def _serialize_task(project, qc_labels):
    stage_slug, stage_label = _stage(project.state)
    return {
        "id": project.id,
        "reference": project.name or "",
        "category": _category_name(project),
        "sub_category": _sub_category_name(project),
        "topic": project.topic_name or "",
        "duration_seconds": project.duration_seconds or 0.0,
        "duration": _fmt_duration(project.duration_seconds),
        "assigned_to_id": project.assigned_to.id or 0,
        "assigned_to": project.assigned_to.name or "",
        "assigned_initials": _initials(project.assigned_to.name),
        "stage": stage_slug,
        "stage_label": stage_label,
        "qc": project.llm_qc_result or "",
        "qc_label": qc_labels.get(project.llm_qc_result, "") or "",
        "cost": round(project.llm_qc_cost_usd or 0.0, 4),
        "updated": _iso(project.write_date),
    }


COLUMNS = [
    {"key": "reference", "label": "Reference", "type": "string"},
    {"key": "category", "label": "Category / Sub-Category", "type": "string"},
    {"key": "topic", "label": "Topic", "type": "string"},
    {"key": "duration", "label": "Duration", "type": "string"},
    {"key": "assigned_to", "label": "Assigned", "type": "string"},
    {"key": "stage", "label": "Stage", "type": "string"},
    {"key": "qc", "label": "QC", "type": "string"},
    {"key": "cost", "label": "Cost", "type": "currency"},
    {"key": "updated", "label": "Updated", "type": "datetime"},
]


class CrowleySourcingTaskViewDashboardController(http.Controller):

    @http.route(
        "/api/v1/crowley_sourcing_ext/task_view_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def crowley_sourcing_ext_task_view_dashboard(self, **kwargs):
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Crowley Sourcing task view.",
                status=403,
            )

        params = kwargs or {}
        filter_domain, err = _build_task_view_domain(env, params)
        if err:
            return err
        order, err = _resolve_order(params)
        if err:
            return err

        _tag, scope, _projects = _scope(env)
        domain = scope + filter_domain

        page = _coerce_int(params.get("page"), 1) or 1
        limit = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT) or LIST_DEFAULT_LIMIT
        if limit > LIST_MAX_LIMIT:
            limit = LIST_MAX_LIMIT
        offset = (page - 1) * limit if page > 0 else 0

        Project = env["video.editor.project"].sudo()
        qc_labels = dict(Project._fields["llm_qc_result"].selection)
        total = Project.search_count(domain)
        records = Project.search(domain, limit=limit, offset=offset, order=order)
        rows = [_serialize_task(rec, qc_labels) for rec in records]

        data = {
            "role": role_tag,
            "columns": COLUMNS,
            "rows": rows,
            "total_records": total,
            "page": page,
            "limit": limit,
        }
        return return_Response(
            message="Task view fetched successfully.",
            status=200,
            data=data,
        )
