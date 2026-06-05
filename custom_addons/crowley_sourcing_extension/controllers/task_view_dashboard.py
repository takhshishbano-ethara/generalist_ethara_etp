from datetime import datetime

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)
from odoo.addons.video_editor_s3.models.video_editor_project import (
    CATEGORIES as CATEGORY_SELECTION,
)

from .analytics_dashboard import _user_role_tag

LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 500

_STATE_IN_FLIGHT = ("processing", "exporting")

STAGE_SELECTION = (
    ("s1_draft", "S1 Draft"),
    ("s2_enriching", "S2 Enriching"),
    ("s2_qc", "S2 QC"),
    ("failed", "Failed"),
)

STAGE_TO_STATES = {
    "s1_draft": ("draft",),
    "s2_enriching": _STATE_IN_FLIGHT,
    "s2_qc": ("processed",),
    "failed": ("error",),
}

STATUS_SELECTION = (
    ("unstarted", "unstarted"),
    ("generating", "generating"),
    ("pending_review", "pending_review"),
    ("approved", "approved"),
    ("failed_qc", "failed_qc"),
)

CATEGORY_SLUG_TO_LABEL = dict(CATEGORY_SELECTION)
CATEGORY_LABEL_TO_SLUG = {label.lower(): slug for slug, label in CATEGORY_SELECTION}


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


def _team_user_ids_for(env, team_field):
    user = env.user
    Employee = env["hr.employee"].sudo()
    Project = env["project.project"].sudo()
    employee = Employee.search([("user_id", "=", user.id)], limit=1)
    if not employee:
        return []
    projects = Project.search([(team_field, "in", employee.ids)])
    taskers = projects.mapped("project_tasker").filtered("task_forge_active")
    user_ids = (taskers.mapped("user_id") | user).ids
    return user_ids


def _user_scope_domain(env, tag):
    user = env.user
    if tag == "full":
        return []
    if tag == "pl":
        ids = _team_user_ids_for(env, "project_lead")
        if not ids:
            return [("assigned_to", "=", user.id)]
        return [("assigned_to", "in", ids)]
    if tag == "qr":
        ids = _team_user_ids_for(env, "project_qc_reviewer")
        if not ids:
            return [("assigned_to", "=", user.id)]
        return [("assigned_to", "in", ids)]
    return [("assigned_to", "=", user.id)]


def _resolve_category_param(raw):
    if not raw:
        return None
    slug = raw.strip()
    if not slug:
        return None
    if slug in CATEGORY_SLUG_TO_LABEL:
        return slug
    lookup = CATEGORY_LABEL_TO_SLUG.get(slug.lower())
    return lookup


def _or_join(leaves):
    if not leaves:
        return []
    if len(leaves) == 1:
        return list(leaves)
    return ["|"] * (len(leaves) - 1) + list(leaves)


def _parse_date(raw, label):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, _error_response(
            f"Invalid {label} '{raw}'. Expected YYYY-MM-DD."
        )


def _date_filter_domain(params):
    domain = []
    start_raw = (params.get("start_date") or "").strip()
    end_raw = (params.get("end_date") or "").strip()
    if start_raw:
        start_date, err = _parse_date(start_raw, "start_date")
        if err:
            return None, err
        domain.append(
            ("create_date", ">=", datetime.combine(start_date, datetime.min.time()))
        )
    if end_raw:
        end_date, err = _parse_date(end_raw, "end_date")
        if err:
            return None, err
        domain.append(
            ("create_date", "<=", datetime.combine(end_date, datetime.max.time()))
        )
    return domain, None


def _status_domain_for(status_slug):
    if status_slug == "unstarted":
        return [("state", "=", "draft")]
    if status_slug == "generating":
        return [("state", "in", list(_STATE_IN_FLIGHT))]
    if status_slug == "pending_review":
        return [
            "|",
            ("state", "=", "processed"),
            "&",
            ("state", "=", "exported"),
            ("review_status", "in", (False, "pending")),
        ]
    if status_slug == "approved":
        return ["&", ("state", "=", "exported"), ("review_status", "=", "approved")]
    if status_slug == "failed_qc":
        return [
            "|",
            ("state", "=", "error"),
            "&",
            ("state", "=", "exported"),
            ("review_status", "=", "rejected"),
        ]
    return []


def _build_task_view_domain(env, params):
    domain, err = _date_filter_domain(params)
    if err:
        return None, err

    raw_category = (params.get("category") or "").strip()
    if raw_category:
        slug = _resolve_category_param(raw_category)
        if not slug:
            return None, _error_response(
                f"Invalid category '{raw_category}'."
            )
        domain.append(("category", "=", slug))

    raw_ql = (params.get("ql") or "").strip()
    if raw_ql:
        ql_id = _coerce_int(raw_ql, 0)
        if not ql_id:
            return None, _error_response(f"Invalid ql '{raw_ql}'.")
        domain.append(("assigned_to", "=", ql_id))

    raw_stage = (params.get("stage") or "").strip()
    if raw_stage:
        if raw_stage not in STAGE_TO_STATES:
            return None, _error_response(f"Invalid stage '{raw_stage}'.")
        domain.append(("state", "in", list(STAGE_TO_STATES[raw_stage])))

    raw_status = (params.get("status") or "").strip()
    if raw_status:
        valid = {key for key, _ in STATUS_SELECTION}
        if raw_status not in valid:
            return None, _error_response(f"Invalid status '{raw_status}'.")
        domain.extend(_status_domain_for(raw_status))

    raw_search = (params.get("search") or "").strip()
    if raw_search:
        search = raw_search
        leaves = [
            ("name", "ilike", search),
            ("prompt", "ilike", search),
            ("llm_fixed_prompt", "ilike", search),
            ("assigned_to.name", "ilike", search),
            ("youtube_url", "ilike", search),
        ]
        domain.extend(_or_join(leaves))

    return domain, None


def _derive_stage(state):
    if state == "draft":
        return "s1_draft", "S1 Draft"
    if state in _STATE_IN_FLIGHT:
        return "s2_enriching", "S2 Enriching"
    if state in ("processed", "exported"):
        return "s2_qc", "S2 QC"
    if state == "error":
        return "failed", "Failed"
    return "", ""


def _derive_status(state, review_status):
    if state == "draft":
        return "unstarted"
    if state in _STATE_IN_FLIGHT:
        return "generating"
    if state == "processed":
        return "pending_review"
    if state == "exported":
        if review_status == "approved":
            return "approved"
        if review_status == "rejected":
            return "failed_qc"
        return "pending_review"
    if state == "error":
        return "failed_qc"
    return ""


def _spec_string(project):
    res = project.resolution or "-"
    dur = (
        f"{int(project.duration_seconds)}s"
        if project.duration_seconds
        else "-"
    )
    return f"{res} · {dur}"


def _prompts(project):
    raw = (project.prompt or "").strip()
    golden = (project.llm_fixed_prompt or "").strip()
    if raw and golden and raw == golden:
        golden = ""
    if not raw and golden:
        raw, golden = golden, ""
    return raw, golden


def _serialize_task(project):
    state = project.state or ""
    review = project.review_status or ""
    stage_slug, stage_label = _derive_stage(state)
    raw_prompt, golden_prompt = _prompts(project)
    status = _derive_status(state, review)

    return {
        "id": project.id,
        "seq": project.name or "",
        "spec": _spec_string(project),
        "raw_prompt": raw_prompt,
        "golden_prompt": golden_prompt,
        "is_enriched": bool(golden_prompt),
        "assigned_ql_id": project.assigned_to.id or False,
        "assigned_ql_name": project.assigned_to.name or "",
        "category_slug": project.category or "",
        "category": CATEGORY_SLUG_TO_LABEL.get(project.category, ""),
        "stage": stage_slug,
        "stage_label": stage_label,
        "status": status,
        "state": state,
        "review_state": review,
        "cost_usd": float(project.llm_qc_cost_usd or 0.0),
        "attempts_used": 0,
        "attempts_remaining": 0,
        "updated_at": _iso(project.write_date),
        "created_at": _iso(project.create_date),
    }


COLUMNS = [
    {"key": "seq", "label": "ID", "type": "string"},
    {"key": "raw_prompt", "label": "Raw Prompt", "type": "string"},
    {"key": "golden_prompt", "label": "Golden Prompt", "type": "string"},
    {"key": "assigned_ql_name", "label": "Assigned", "type": "string"},
    {"key": "category", "label": "Category", "type": "string"},
    {"key": "stage_label", "label": "Stage", "type": "string"},
    {"key": "status", "label": "Status", "type": "string"},
    {"key": "cost_usd", "label": "Cost (USD)", "type": "currency"},
    {"key": "updated_at", "label": "Updated", "type": "datetime"},
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
        scope_domain = _user_scope_domain(env, role_tag)
        filter_domain, err = _build_task_view_domain(env, params)
        if err:
            return err
        domain = scope_domain + filter_domain

        page = _coerce_int(params.get("page"), 1) or 1
        limit = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT) or LIST_DEFAULT_LIMIT
        if limit > LIST_MAX_LIMIT:
            limit = LIST_MAX_LIMIT
        offset = (page - 1) * limit if page > 0 else 0

        Project = env["video.editor.project"].sudo()
        total = Project.search_count(domain)
        records = Project.search(
            domain, limit=limit, offset=offset, order="write_date desc, id desc"
        )
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
