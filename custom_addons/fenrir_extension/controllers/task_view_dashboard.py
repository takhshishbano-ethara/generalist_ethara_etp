from datetime import datetime

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    DONE_STATES,
    FAILED_STATES,
    IN_FLIGHT_STATES,
    _create_date_domain,
    _parse_date,
    _scope,
    _user_role_tag,
    _task_cost,
)

LIST_DEFAULT_LIMIT = 25
LIST_MAX_LIMIT = 200

# Stage badge shown in the Tasks table (Draft / Processed / Done / Failed),
# derived from the raw fenrir.task status.
STAGE_TO_STATES = {
    "draft": ("draft",),
    "processed": IN_FLIGHT_STATES,
    "done": DONE_STATES,
    "failed": FAILED_STATES,
}

SORT_FIELDS = {
    "reference": "code",
    "topic": "title",
    "updated": "write_date",
    "cost": "pricing",
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


def _category_name(task):
    return task.category_id.name or "" if task.category_id else ""


def _sub_category_name(task):
    return task.subcategory or ""


def _stage(status):
    if status == "draft":
        return "draft", "Draft"
    if status in IN_FLIGHT_STATES:
        return "processed", "Processed"
    if status in DONE_STATES:
        return "done", "Done"
    if status in FAILED_STATES:
        return "failed", "Failed"
    return status or "", status or ""


def _qc_verdict(task):
    if task.status in DONE_STATES:
        return "pass"
    if task.status in FAILED_STATES:
        return "fail"
    return ""


QC_LABELS = {"pass": "Pass", "fail": "Fail", "": ""}


def _build_task_view_domain(env, params):
    domain = []

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
        domain.append(("status", "in", list(STAGE_TO_STATES[raw_stage])))

    raw_qc = (params.get("qc") or params.get("qc_verdict") or "").strip()
    if raw_qc:
        valid = {"pass", "fail"}
        verdicts = [v.strip() for v in raw_qc.split(",") if v.strip()]
        invalid = [v for v in verdicts if v not in valid]
        if invalid:
            return None, _error_response(
                f"Invalid qc value(s): {', '.join(invalid)}."
            )
        status_filter = []
        if "pass" in verdicts:
            status_filter += list(DONE_STATES)
        if "fail" in verdicts:
            status_filter += list(FAILED_STATES)
        domain.append(("status", "in", status_filter))

    raw_category = (params.get("category") or "").strip()
    if raw_category:
        if raw_category.isdigit():
            domain.append(("category_id", "=", int(raw_category)))
        else:
            domain.append(("category_id.name", "ilike", raw_category))

    raw_sub_category = (params.get("sub_category") or "").strip()
    if raw_sub_category:
        domain.append(("subcategory", "ilike", raw_sub_category))

    raw_assigned = (params.get("assigned") or params.get("tasker") or "").strip()
    if raw_assigned:
        if raw_assigned.isdigit():
            domain.append(("lead_user_id", "=", int(raw_assigned)))
        else:
            domain.append(("lead_user_id.name", "ilike", raw_assigned))

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|",
            "|",
            ("code", "ilike", search),
            ("title", "ilike", search),
            ("overview", "ilike", search),
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


def _serialize_task(task):
    stage_slug, stage_label = _stage(task.status)
    verdict = _qc_verdict(task)
    duration_seconds = (task.estimated_completion_time_hours or 0.0) * 3600.0
    return {
        "id": task.id,
        "reference": task.code or "",
        "category": _category_name(task),
        "sub_category": _sub_category_name(task),
        "topic": task.title or "",
        "duration_seconds": duration_seconds,
        "duration": _fmt_duration(duration_seconds),
        "assigned_to_id": task.lead_user_id.id or 0,
        "assigned_to": task.lead_user_id.name or "",
        "assigned_initials": _initials(task.lead_user_id.name),
        "stage": stage_slug,
        "stage_label": stage_label,
        "qc": verdict,
        "qc_label": QC_LABELS.get(verdict, ""),
        "cost": round(_task_cost(task), 4),
        "updated": _iso(task.write_date),
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


class FenrirTaskViewDashboardController(http.Controller):

    @http.route(
        "/api/v1/fenrir_ext/task_view_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def fenrir_ext_task_view_dashboard(self, **kwargs):
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Fenrir task view.",
                status=403,
            )

        params = kwargs or {}
        filter_domain, err = _build_task_view_domain(env, params)
        if err:
            return err
        order, err = _resolve_order(params)
        if err:
            return err

        _tag, scope, _tasks = _scope(env)
        domain = scope + filter_domain

        page = _coerce_int(params.get("page"), 1) or 1
        limit = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT) or LIST_DEFAULT_LIMIT
        if limit > LIST_MAX_LIMIT:
            limit = LIST_MAX_LIMIT
        offset = (page - 1) * limit if page > 0 else 0

        Task = env["fenrir.task"].sudo()
        total = Task.search_count(domain)
        records = Task.search(domain, limit=limit, offset=offset, order=order)
        rows = [_serialize_task(rec) for rec in records]

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
