from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)
from odoo.addons.crowley.models.crowley_generation import CATEGORY_SELECTION

from .analytics_dashboard import _user_role_tag

LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 500


def _team_user_ids_for(env, team_field):
    user = env.user
    Employee = env["hr.employee"].sudo()
    employee = Employee.search([("user_id", "=", user.id)], limit=1)
    if not employee:
        return []
    Project = env["project.project"].sudo()
    projects = Project.search([(team_field, "in", employee.ids)])
    if not projects:
        return []
    taskers = projects.mapped("project_tasker")
    return list(set((taskers.mapped("user_id") | user).ids))


def _user_scope_domain(env, tag):
    if tag == "full":
        return []
    if tag == "pl":
        user_ids = _team_user_ids_for(env, "project_lead")
        if not user_ids:
            return [("user_id", "=", env.user.id)]
        return [("user_id", "in", user_ids)]
    if tag == "qr":
        user_ids = _team_user_ids_for(env, "project_qc_reviewer")
        if not user_ids:
            return [("user_id", "=", env.user.id)]
        return [("user_id", "in", user_ids)]
    return [("user_id", "=", env.user.id)]


CATEGORY_SLUG_TO_LABEL = dict(CATEGORY_SELECTION)
CATEGORY_LABEL_TO_SLUG = {v.lower(): k for k, v in CATEGORY_SLUG_TO_LABEL.items()}

_STATE_IN_FLIGHT = ("queued", "submitting", "processing", "downloading")

STAGE_SELECTION = (
    ("s1_draft", "S1 Draft"),
    ("s2_enriching", "S2 Enriching"),
    ("s2_qc", "S2 QC"),
    ("failed", "Failed"),
)
STAGE_TO_STATES = {
    "s1_draft": ("draft",),
    "s2_enriching": _STATE_IN_FLIGHT,
    "s2_qc": ("done",),
    "failed": ("failed", "cancelled"),
}

STATUS_SELECTION = (
    ("unstarted", "unstarted"),
    ("generating", "generating"),
    ("pending_review", "pending_review"),
    ("approved", "approved"),
    ("failed_qc", "failed_qc"),
)


def _coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(dt):
    return dt.isoformat() if dt else ""


def _error_response(message, status):
    return return_Response(message=message, status=status, errors=[message])


def _resolve_category_param(raw):
    out = []
    invalid = []
    for piece in (p.strip() for p in str(raw).split(",") if p.strip()):
        if piece in CATEGORY_SLUG_TO_LABEL:
            out.append(piece)
            continue
        slug = CATEGORY_LABEL_TO_SLUG.get(piece.lower())
        if slug:
            out.append(slug)
        else:
            invalid.append(piece)
    if invalid:
        return None, (
            f"Invalid category {invalid}. "
            f"Allowed slugs: {', '.join(CATEGORY_SLUG_TO_LABEL)}."
        )
    return out, None


def _status_domain_for(status_slug):
    if status_slug == "unstarted":
        return [("state", "=", "draft")]
    if status_slug == "generating":
        return [("state", "in", list(_STATE_IN_FLIGHT))]
    if status_slug == "pending_review":
        return [
            "&",
            ("state", "=", "done"),
            ("review_state", "in", (False, "pending")),
        ]
    if status_slug == "approved":
        return [
            "&",
            ("state", "=", "done"),
            ("review_state", "=", "approved"),
        ]
    if status_slug == "failed_qc":
        return [
            "|",
            ("state", "in", ("failed", "cancelled")),
            "&",
            ("state", "=", "done"),
            ("review_state", "=", "rejected"),
        ]
    return []


def _or_join(sub_domains):
    sub_domains = [d for d in sub_domains if d]
    if not sub_domains:
        return []
    if len(sub_domains) == 1:
        return sub_domains[0]
    out = ["|"] * (len(sub_domains) - 1)
    for d in sub_domains:
        out += d
    return out


def _build_task_view_domain(env, params):
    domain = []

    raw_start = (params.get("start_date") or "").strip()
    if raw_start:
        try:
            start = datetime.strptime(raw_start, "%Y-%m-%d").date()
        except ValueError:
            return None, _error_response(
                f"Invalid start_date '{raw_start}'. Expected YYYY-MM-DD.",
                status=400,
            )
        domain.append(
            ("create_date", ">=", datetime.combine(start, datetime.min.time()))
        )

    raw_end = (params.get("end_date") or "").strip()
    if raw_end:
        try:
            end = datetime.strptime(raw_end, "%Y-%m-%d").date()
        except ValueError:
            return None, _error_response(
                f"Invalid end_date '{raw_end}'. Expected YYYY-MM-DD.",
                status=400,
            )
        domain.append(
            (
                "create_date",
                "<",
                datetime.combine(end, datetime.min.time()) + timedelta(days=1),
            )
        )

    raw_cat = (params.get("category") or "").strip()
    if raw_cat:
        slugs, err = _resolve_category_param(raw_cat)
        if err:
            return None, _error_response(err, status=400)
        if slugs:
            domain.append(("category", "in", slugs))

    raw_ql = (params.get("ql") or "").strip()
    if raw_ql:
        if raw_ql.isdigit():
            domain.append(("user_id", "=", int(raw_ql)))
        else:
            domain.append(("user_id.name", "ilike", raw_ql))

    stage_raw = (params.get("stage") or "").strip()
    if stage_raw:
        allowed = dict(STAGE_SELECTION)
        requested = [s.strip() for s in stage_raw.split(",") if s.strip()]
        invalid = [s for s in requested if s not in allowed]
        if invalid:
            return None, _error_response(
                f"Invalid stage {invalid}. Allowed: {', '.join(allowed)}.",
                status=400,
            )
        states = []
        for s in requested:
            states += list(STAGE_TO_STATES[s])
        if states:
            domain.append(("state", "in", states))

    status_raw = (params.get("status") or "").strip()
    if status_raw:
        allowed = dict(STATUS_SELECTION)
        requested = [s.strip() for s in status_raw.split(",") if s.strip()]
        invalid = [s for s in requested if s not in allowed]
        if invalid:
            return None, _error_response(
                f"Invalid status {invalid}. Allowed: {', '.join(allowed)}.",
                status=400,
            )
        sub_domains = [_status_domain_for(s) for s in requested]
        domain += _or_join(sub_domains)

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|", "|", "|", "|",
            ("name", "ilike", search),
            ("prompt", "ilike", search),
            ("original_prompt", "ilike", search),
            ("user_id.name", "ilike", search),
            ("openrouter_job_id", "ilike", search),
        ]

    return domain, None


def _derive_stage(state):
    if state == "draft":
        return "s1_draft", "S1 Draft"
    if state in _STATE_IN_FLIGHT:
        return "s2_enriching", "S2 Enriching"
    if state == "done":
        return "s2_qc", "S2 QC"
    if state in ("failed", "cancelled"):
        return "failed", "Failed"
    return state or "", state or ""


def _derive_status(state, review_state):
    if state == "draft":
        return "unstarted"
    if state in _STATE_IN_FLIGHT:
        return "generating"
    if state == "done":
        if review_state == "approved":
            return "approved"
        if review_state == "rejected":
            return "failed_qc"
        return "pending_review"
    if state in ("failed", "cancelled"):
        return "failed_qc"
    return state or ""


def _spec_string(job):
    res = job.resolution or "-"
    ar = job.aspect_ratio or "-"
    dur = f"{job.duration}s" if job.duration else "-"
    return f"{res} · {ar} · {dur}"


def _prompts(job):
    raw = (job.original_prompt or "").strip()
    golden = (job.prompt or "").strip()
    if not raw:
        raw = golden
        golden = ""
    elif raw == golden:
        golden = ""
    return raw, golden


def _serialize_task(job):
    state = job.state or ""
    review = job.review_state or ""
    stage_slug, stage_label = _derive_stage(state)
    raw_prompt, golden_prompt = _prompts(job)
    return {
        "id": job.id,
        "seq": job.name or "",
        "spec": _spec_string(job),
        "raw_prompt": raw_prompt,
        "golden_prompt": golden_prompt,
        "is_enriched": bool(golden_prompt),
        "assigned_ql_id": job.user_id.id or False,
        "assigned_ql_name": job.user_id.name or "",
        "category_slug": job.category or "",
        "category": CATEGORY_SLUG_TO_LABEL.get(job.category, ""),
        "stage": stage_slug,
        "stage_label": stage_label,
        "status": _derive_status(state, review),
        "state": state,
        "review_state": review,
        "cost_usd": float(job.cost_usd or 0.0),
        "attempts_used": job.attempts_used or 0,
        "attempts_remaining": job.attempts_remaining or 0,
        "updated_at": _iso(job.write_date),
        "created_at": _iso(job.create_date),
    }


class CrowleyTaskViewDashboardController(http.Controller):

    @http.route(
        "/api/v1/crowley_ext/task_view_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def crowley_ext_task_view_dashboard(self, **kwargs):
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Crowley task view.",
                status=403,
            )
        scope_domain = _user_scope_domain(env, role_tag)
        params = request.params or {}

        domain, error = _build_task_view_domain(env, params)
        if error is not None:
            return error
        domain = scope_domain + domain

        page = max(1, _coerce_int(params.get("page"), 1))
        limit = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT)
        limit = max(1, min(limit, LIST_MAX_LIMIT))
        offset = (page - 1) * limit

        Job = env["crowley.generation"].sudo()
        total = Job.search_count(domain)
        records = Job.search(
            domain,
            limit=limit,
            offset=offset,
            order="write_date desc, id desc",
        )

        tasks = [_serialize_task(j) for j in records]
        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag,
                "columns": [
                    {"key": "seq", "label": "Reference", "type": "string"},
                    {"key": "raw_prompt", "label": "Raw Prompt", "type": "string"},
                    {"key": "golden_prompt", "label": "Golden Prompt", "type": "string"},
                    {"key": "assigned_ql_name", "label": "QL", "type": "string"},
                    {"key": "category", "label": "Category", "type": "string"},
                    {"key": "stage_label", "label": "Stage", "type": "string"},
                    {"key": "status", "label": "Status", "type": "string"},
                    {"key": "cost_usd", "label": "Cost", "type": "currency"},
                    {"key": "updated_at", "label": "Updated", "type": "datetime"},
                ],
                "rows": tasks,
                "total_records": total,
                "page": page,
                "limit": limit,
            },
        )
