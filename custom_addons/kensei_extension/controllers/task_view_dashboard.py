from datetime import datetime, timedelta

from odoo import fields, http
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
    "completed_date": "batch_completed_at",
    "task_id": "task_id",
}

COLUMNS = (
    {"key": "task_id", "label": "Task ID", "type": "string"},
    {"key": "task_status", "label": "Status", "type": "selection"},
    {"key": "qc_status", "label": "QC Status", "type": "selection"},
    {"key": "task_type", "label": "Task Type", "type": "selection"},
    {"key": "difficulty", "label": "Difficulty", "type": "selection"},
    {"key": "l1_classification", "label": "L1", "type": "string"},
    {"key": "l2_classification", "label": "L2", "type": "string"},
    {"key": "parsona", "label": "Parsona", "type": "string"},
    {"key": "tasker_name", "label": "Tasker", "type": "string"},
    {"key": "created_date", "label": "Created", "type": "datetime"},
    {"key": "completed_date", "label": "Completed", "type": "datetime"},
)


def _coerce_int(raw, default):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _parse_selection(model, field_name, raw, label):
    if not raw:
        return [], None
    selection = dict(model._fields[field_name].selection or [])
    values = [v.strip() for v in raw.split(",") if v.strip()]
    invalid = [v for v in values if v not in selection]
    if invalid:
        return [], return_Response(
            message="Invalid %s filter." % label,
            status=400,
            errors=["invalid_%s" % label, ",".join(invalid)],
        )
    return values, None


def _build_task_view_domain(env, params):
    model = env["kensei2.kensei2"]
    start, err = _parse_date(params.get("start_date"), "start_date")
    if err:
        return None, err
    end, err = _parse_date(params.get("end_date"), "end_date")
    if err:
        return None, err
    status_values, err = _parse_selection(
        model, "task_status", params.get("status"), "status"
    )
    if err:
        return None, err
    qc_values, err = _parse_selection(
        model, "qc_status", params.get("qc_status"), "qc_status"
    )
    if err:
        return None, err
    task_type_values, err = _parse_selection(
        model, "task_type", params.get("task_type"), "task_type"
    )
    if err:
        return None, err
    difficulty_values, err = _parse_selection(
        model, "difficulty", params.get("difficulty"), "difficulty"
    )
    if err:
        return None, err
    domain = _create_date_domain(start, end)
    if status_values:
        domain.append(("task_status", "in", status_values))
    if qc_values:
        domain.append(("qc_status", "in", qc_values))
    if task_type_values:
        domain.append(("task_type", "in", task_type_values))
    if difficulty_values:
        domain.append(("difficulty", "in", difficulty_values))
    l1_raw = params.get("l1_classification")
    if l1_raw:
        l1_id = _coerce_int(l1_raw, None)
        if l1_id is None:
            return None, return_Response(
                message="Invalid l1_classification.",
                status=400,
                errors=["invalid_l1_classification"],
            )
        domain.append(("l1_classification", "=", l1_id))
    persona_raw = params.get("persona_id")
    if persona_raw:
        persona_id = _coerce_int(persona_raw, None)
        if persona_id is None:
            return None, return_Response(
                message="Invalid persona_id.",
                status=400,
                errors=["invalid_persona_id"],
            )
        domain.append(("persona_id", "=", persona_id))
    tasker_raw = params.get("tasker_id")
    if tasker_raw:
        tasker_id = _coerce_int(tasker_raw, None)
        if tasker_id is None:
            return None, return_Response(
                message="Invalid tasker_id.",
                status=400,
                errors=["invalid_tasker_id"],
            )
        domain.append(("employee_id", "=", tasker_id))
    search = params.get("search")
    if search:
        domain = ["&"] + domain + [("task_id", "ilike", search)]
    return domain, None


def _resolve_order(params):
    sort_by = params.get("sort_by") or "created_date"
    sort_order = (params.get("sort_order") or "desc").lower()
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"
    field = SORT_FIELDS.get(sort_by, SORT_FIELDS["created_date"])
    return "%s %s, id desc" % (field, sort_order)


def _serialize(task, qc_labels, status_labels, task_type_labels, difficulty_labels):
    employee = task.employee_id
    return {
        "id": task.id,
        "task_id": task.task_id or "",
        "task_status": task.task_status or "",
        "task_status_label": status_labels.get(task.task_status, ""),
        "qc_status": task.qc_status or "",
        "qc_status_label": qc_labels.get(task.qc_status, ""),
        "task_type": task.task_type or "",
        "task_type_label": task_type_labels.get(task.task_type, ""),
        "difficulty": task.difficulty or "",
        "difficulty_label": difficulty_labels.get(task.difficulty, ""),
        "l1_classification_id": task.l1_classification.id if task.l1_classification else 0,
        "l1_classification": task.l1_classification.name if task.l1_classification else "",
        "l2_classification_id": task.l2_classification.id if task.l2_classification else 0,
        "l2_classification": task.l2_classification.name if task.l2_classification else "",
        "parsona_id": task.parsona.id if task.parsona else 0,
        "parsona": task.parsona.name if task.parsona else "",
        "persona_id": task.persona_id.id if task.persona_id else 0,
        "persona_name": task.persona_id.name if task.persona_id else "",
        "tasker_id": employee.id if employee else 0,
        "tasker_name": employee.name if employee else "",
        "created_date": fields.Datetime.to_string(task.create_date) if task.create_date else "",
        "completed_date": fields.Datetime.to_string(task.batch_completed_at) if task.batch_completed_at else "",
    }


class KenseiTaskViewDashboardController(http.Controller):

    @http.route(
        "/api/v1/kensei_ext/task_view_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def kensei_ext_task_view_dashboard(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Kensei task view.",
                status=403,
                errors=["forbidden"],
            )
        params = request.params
        _tag, scope_domain, _projects = _scope(env)
        filter_domain, err = _build_task_view_domain(env, params)
        if err:
            return err
        domain = list(scope_domain) + filter_domain
        model = env["kensei2.kensei2"].sudo()
        total = model.search_count(domain)
        limit = min(_coerce_int(params.get("limit"), DEFAULT_LIMIT), MAX_LIMIT)
        if limit < 1:
            limit = DEFAULT_LIMIT
        page = max(_coerce_int(params.get("page"), 1), 1)
        offset = (page - 1) * limit
        order = _resolve_order(params)
        records = model.search(domain, order=order, limit=limit, offset=offset)
        qc_labels = dict(model._fields["qc_status"].selection or [])
        status_labels = dict(model._fields["task_status"].selection or [])
        task_type_labels = dict(model._fields["task_type"].selection or [])
        difficulty_labels = dict(model._fields["difficulty"].selection or [])
        rows = [
            _serialize(r, qc_labels, status_labels, task_type_labels, difficulty_labels)
            for r in records
        ]
        total_pages = (total + limit - 1) // limit if limit else 1
        payload = {
            "columns": list(COLUMNS),
            "rows": rows,
            "pagination": {
                "total_records": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
            },
        }
        return return_Response(message="Success", status=200, data=payload)
