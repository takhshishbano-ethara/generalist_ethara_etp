from datetime import datetime, timedelta
from urllib.parse import urlparse

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics import COMPLETED_STATES, _user_role_tag
from .main import (
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    _coerce_int,
    _iso,
    _job_scope_domain,
    _require_leviathan_user,
)


STATE_BADGE = {
    "draft": ("Created", "muted"),
    "not_assigned": ("Created", "muted"),
    "extracting": ("Scraping", "info"),
    "extracted": ("Generated", "warn"),
    "generating": ("Generated", "warn"),
    "generated": ("Generated", "warn"),
    "scoring": ("In Review", "primary"),
    "scored": ("In Review", "primary"),
    "qc_running": ("In Review", "primary"),
    "done": ("Submitted", "success"),
    "submitted": ("Submitted", "success"),
    "failed": ("Scrape Failed", "danger"),
    "discarded": ("Failed", "danger"),
    "cancelled": ("Failed", "danger"),
}

QC_VERDICT_BADGE = {
    "shippable": ("Pass", "success"),
    "fixes": ("Warning", "warn"),
    "not_shippable": ("Fail", "danger"),
}

CATEGORY_COLOR_TOKENS = ("muted", "primary", "info", "warn", "success", "danger")


def _s3_base_url(env):
    icp = env["ir.config_parameter"].sudo()
    cdn_url = icp.get_param("leviathan.s3_cdn_url", "")
    bucket = icp.get_param("leviathan.s3_bucket", "")
    region = icp.get_param("leviathan.s3_region", "us-east-1")
    if cdn_url:
        return cdn_url.rstrip("/")
    if bucket:
        return f"https://{bucket}.s3.{region}.amazonaws.com"
    return ""


def _domain_from_url(raw):
    if not raw:
        return ""
    try:
        host = urlparse(raw).netloc or urlparse(raw).path
    except Exception:
        return raw
    return (host or "").lstrip("www.")


def _format_short_date(dt):
    if not dt:
        return ""
    return f"{dt.strftime('%b')} {dt.day}"


def _format_long_date(dt):
    if not dt:
        return ""
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _category_color_token(category_id):
    if not category_id:
        return "muted"
    return CATEGORY_COLOR_TOKENS[category_id % len(CATEGORY_COLOR_TOKENS)]


def _state_badge(state):
    label, color = STATE_BADGE.get(state or "", (state or "", "muted"))
    return {"key": state or "", "label": label, "color_token": color}


def _qc_badge(qc_verdict, score):
    if not qc_verdict:
        return {"key": "", "label": "—", "color_token": "muted"}
    label_prefix, color = QC_VERDICT_BADGE.get(
        qc_verdict, (qc_verdict, "muted")
    )
    score_int = int(round(score or 0))
    label = f"{label_prefix} · {score_int}" if score else label_prefix
    return {"key": qc_verdict, "label": label, "color_token": color}


def _source_badge(via_batch):
    if via_batch:
        return {"key": "bulk", "label": "Bulk Excel", "color_token": "info"}
    return {"key": "single", "label": "Single", "color_token": "muted"}


ROLE_BADGE = {
    "tpm": ("TPM", "warn"),
    "pl": ("PL", "primary"),
    "qr": ("QL", "info"),
    "tasker": ("Tasker", "success"),
    "aire": ("AIRE", "muted"),
    "swe": ("SWE", "muted"),
}

STATUS_BADGE = {
    "online": ("Online", "success"),
    "away": ("Away", "warn"),
    "offline": ("Offline", "muted"),
}


def _role_badge(tag):
    label, color = ROLE_BADGE.get(tag, ((tag or "").upper(), "muted"))
    return {"key": tag or "", "label": label, "color_token": color}


def _status_badge(im_status):
    key = (im_status or "offline").lower()
    label, color = STATUS_BADGE.get(key, (key.title(), "muted"))
    return {"key": key, "label": label, "color_token": color}


def _initials(name):
    if not name:
        return ""
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _format_tasks_done(count):
    return str(count) if count else "—"


def _build_task_view_domain(env, params):
    domain = []

    urls_added = (params.get("urls_added") or "").strip().lower()
    if urls_added in ("1", "true", "yes"):
        domain.append(("url", "!=", False))

    raw_start = (params.get("start_date") or "").strip()
    if raw_start:
        try:
            start = datetime.strptime(raw_start, "%Y-%m-%d").date()
        except ValueError:
            return None, return_Response(
                message=f"Invalid start_date '{raw_start}'. Expected YYYY-MM-DD.",
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
            return None, return_Response(
                message=f"Invalid end_date '{raw_end}'. Expected YYYY-MM-DD.",
                status=400,
            )
        domain.append(
            (
                "create_date",
                "<",
                datetime.combine(end, datetime.min.time()) + timedelta(days=1),
            )
        )

    category = (params.get("category") or "").strip()
    if category:
        if category.isdigit():
            domain.append(("category_id", "=", int(category)))
        else:
            domain.append(("category_id.name", "=ilike", category))

    assigned_ql = (params.get("assigned_ql") or "").strip()
    if assigned_ql:
        if assigned_ql.isdigit():
            emp_domain = [("task_forge_qr_id", "=", int(assigned_ql))]
        else:
            emp_domain = [("task_forge_qr_id.name", "ilike", assigned_ql)]
        ql_user_ids = (
            env["hr.employee"].sudo().search(emp_domain).mapped("user_id").ids
        )
        domain.append(("user_id", "in", ql_user_ids))

    assigned_tasker = (params.get("assigned_tasker") or "").strip()
    if assigned_tasker:
        if assigned_tasker.isdigit():
            domain.append(("user_id", "=", int(assigned_tasker)))
        else:
            domain.append(("user_id.name", "ilike", assigned_tasker))

    status = (params.get("status") or "").strip()
    if status:
        valid_states = dict(env["leviathan.job"]._fields["state"].selection)
        requested = [s.strip() for s in status.split(",") if s.strip()]
        invalid = [s for s in requested if s not in valid_states]
        if invalid:
            return None, return_Response(
                message=(
                    f"Invalid status {invalid}. "
                    f"Allowed: {', '.join(valid_states)}."
                ),
                status=400,
            )
        if requested:
            domain.append(("state", "in", requested))

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|", "|", "|",
            ("name", "ilike", search),
            ("site_name", "ilike", search),
            ("category_id.name", "ilike", search),
            ("create_uid.name", "ilike", search),
        ]

    return domain, None


def _build_urls_added_domain(env, params):
    domain = [("url", "!=", False)]

    raw_start = (params.get("start_date") or "").strip()
    if raw_start:
        try:
            start = datetime.strptime(raw_start, "%Y-%m-%d").date()
        except ValueError:
            return None, return_Response(
                message=f"Invalid start_date '{raw_start}'. Expected YYYY-MM-DD.",
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
            return None, return_Response(
                message=f"Invalid end_date '{raw_end}'. Expected YYYY-MM-DD.",
                status=400,
            )
        domain.append(
            (
                "create_date",
                "<",
                datetime.combine(end, datetime.min.time()) + timedelta(days=1),
            )
        )

    category = (params.get("category") or "").strip()
    if category:
        if category.isdigit():
            domain.append(("category_id", "=", int(category)))
        else:
            domain.append(("category_id.name", "=ilike", category))

    added_by = (params.get("added_by") or "").strip()
    if added_by:
        if added_by.isdigit():
            domain.append(("create_uid", "=", int(added_by)))
        else:
            domain.append(("create_uid.name", "ilike", added_by))

    source = (params.get("source") or "").strip().lower()
    if source in ("single", "0", "false"):
        domain.append(("via_batch", "=", False))
    elif source in ("bulk", "bulk_excel", "1", "true"):
        domain.append(("via_batch", "=", True))

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|", "|", "|",
            ("name", "ilike", search),
            ("url", "ilike", search),
            ("site_name", "ilike", search),
            ("create_uid.name", "ilike", search),
        ]

    return domain, None


def _resolve_team_project(env, params):
    raw = (params.get("project_id") or "").strip()
    if not raw:
        return None, return_Response(message="project_id is required.", status=400)
    try:
        pid = int(raw)
    except ValueError:
        return None, return_Response(
            message=f"Invalid project_id '{raw}'.", status=400
        )
    project = env["project.project"].sudo().browse(pid)
    if not project.exists():
        return None, return_Response(message=f"Project {pid} not found.", status=404)
    return project, None


def _collect_team_members(project):
    members_by_id = {}
    for emp in project.project_lead.mapped("task_forge_tpm_id"):
        if emp.id and emp.id not in members_by_id:
            members_by_id[emp.id] = (emp, "tpm")
    for tag, employees in (
        ("pl", project.project_lead),
        ("qr", project.project_qc_reviewer),
        ("tasker", project.project_tasker),
        ("aire", project.project_aire),
        ("swe", project.project_swe),
    ):
        for emp in employees:
            if emp.id and emp.id not in members_by_id:
                members_by_id[emp.id] = (emp, tag)
    return list(members_by_id.values())


def _serialize_task(job, base_url):
    asset_keys = job.asset_keys or []
    asset_links = [f"{base_url}/{key}" for key in asset_keys] if base_url else []
    assets_count = len(asset_keys)
    qr_name = ""
    if job.user_id and job.user_id.employee_id:
        qr_name = job.user_id.employee_id.task_forge_qr_id.name or ""
    return {
        "id": job.id,
        "task": {
            "top": job.site_name or _domain_from_url(job.url) or "",
            "bottom": job.name or "",
        },
        "category": job.category_id.name or "",
        "status": _state_badge(job.state),
        "qc": _qc_badge(job.qc_verdict, job.score),
        "assets": f"{assets_count} files" if assets_count else "—",
        "assigned_ql": qr_name or "Unassigned",
        "created": _format_short_date(job.create_date),
        "seq": job.name or "",
        "state": job.state or "",
        "qc_verdict": job.qc_verdict or "",
        "score": job.score or 0.0,
        "category_id": job.category_id.id or False,
        "assets_count": assets_count,
        "asset_file_links": asset_links,
        "assigned_tasker_name": job.user_id.name or "",
        "source": job.url or "",
        "created_at": _iso(job.create_date),
        "created_by": job.create_uid.name or "",
    }


def _serialize_url_row(job):
    return {
        "id": job.id,
        "website_url": {
            "label": job.site_name or _domain_from_url(job.url) or "",
            "href": job.url or "",
        },
        "category": {
            "key": str(job.category_id.id or ""),
            "label": job.category_id.name or "",
            "color_token": _category_color_token(job.category_id.id or 0),
        },
        "added_by": job.create_uid.name or "",
        "source": _source_badge(bool(job.via_batch)),
        "date_added": _format_long_date(job.create_date),
        "seq": job.name or "",
        "category_id": job.category_id.id or False,
        "added_by_id": job.create_uid.id or False,
        "via_batch": bool(job.via_batch),
        "created_at": _iso(job.create_date),
    }


TASK_VIEW_COLUMNS = [
    {"key": "task", "label": "Task", "type": "composite", "width": "fill"},
    {"key": "category", "label": "Category", "type": "string", "width": 180},
    {"key": "status", "label": "Status", "type": "badge", "width": 150},
    {"key": "qc", "label": "QC", "type": "badge", "width": 120},
    {"key": "assets", "label": "Assets", "type": "string", "width": 90},
    {"key": "assigned_ql", "label": "Assigned QL", "type": "string", "width": 150},
    {"key": "created", "label": "Created", "type": "date", "width": 90},
]


URLS_ADDED_COLUMNS = [
    {"key": "website_url", "label": "Website URL", "type": "url", "width": "fill"},
    {"key": "category", "label": "Category", "type": "badge", "width": 200},
    {"key": "added_by", "label": "Added by", "type": "string", "width": 150},
    {"key": "source", "label": "Source", "type": "badge", "width": 110},
    {"key": "date_added", "label": "Date added", "type": "date", "width": 110},
]


TEAM_VIEW_COLUMNS = [
    {"key": "member", "label": "Name", "type": "composite", "width": "fill"},
    {"key": "role", "label": "Role", "type": "badge", "width": 110},
    {"key": "status", "label": "Status", "type": "badge", "width": 100},
    {"key": "tasks_done", "label": "Tasks Done", "type": "string", "width": 90},
    {"key": "actions", "label": "Actions", "type": "actions", "width": 60},
]


def _serialize_team_member(env, employee, role_tag):
    user = employee.user_id
    name = employee.name or (user.name if user else "")
    email = (user.email if user else "") or employee.work_email or ""
    im_status = getattr(user, "im_status", "offline") if user else "offline"
    tasks_done = 0
    if user:
        tasks_done = env["leviathan.job"].sudo().search_count([
            ("user_id", "=", user.id),
            ("state", "in", list(COMPLETED_STATES)),
        ])
    return {
        "id": employee.id,
        "member": {"avatar": _initials(name), "top": name, "bottom": email},
        "role": _role_badge(role_tag),
        "status": _status_badge(im_status),
        "tasks_done": _format_tasks_done(tasks_done),
        "actions": "",
        "employee_id": employee.id,
        "user_id": user.id if user else False,
        "name": name,
        "email": email,
        "role_key": role_tag,
        "tasks_done_count": tasks_done,
    }


class LeviathanTaskViewController(http.Controller):

    @http.route(
        "/api/v1/leviathan_ext/leviathan_task_view",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def leviathan_task_view(self, **kwargs):
        guard = _require_leviathan_user()
        if guard is not None:
            return guard

        env = request.env
        params = request.params or {}

        domain, error = _build_task_view_domain(env, params)
        if error is not None:
            return error

        domain = _job_scope_domain() + domain

        page = max(1, _coerce_int(params.get("page"), 1))
        limit = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT)
        limit = max(1, min(limit, LIST_MAX_LIMIT))
        offset = (page - 1) * limit

        Job = env["leviathan.job"].sudo()
        total = Job.search_count(domain)
        records = Job.search(
            domain, limit=limit, offset=offset, order="create_date desc, id desc"
        )

        base_url = _s3_base_url(env)
        tasks = [_serialize_task(job, base_url) for job in records]
        return return_Response(
            message="OK",
            status=200,
            data={
                "role": _user_role_tag(env) or "tasker",
                "columns": TASK_VIEW_COLUMNS,
                "rows": tasks,
                "total_records": total,
                "page": page,
                "limit": limit,
            },
        )

    @http.route(
        "/api/v1/leviathan_ext/leviathan_urls_added_task_view",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def leviathan_urls_added_task_view(self, **kwargs):
        guard = _require_leviathan_user()
        if guard is not None:
            return guard

        env = request.env
        params = request.params or {}

        domain, error = _build_urls_added_domain(env, params)
        if error is not None:
            return error

        domain = _job_scope_domain() + domain

        page = max(1, _coerce_int(params.get("page"), 1))
        limit = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT)
        limit = max(1, min(limit, LIST_MAX_LIMIT))
        offset = (page - 1) * limit

        Job = env["leviathan.job"].sudo()
        total = Job.search_count(domain)
        records = Job.search(
            domain, limit=limit, offset=offset, order="create_date desc, id desc"
        )

        rows = [_serialize_url_row(job) for job in records]
        return return_Response(
            message="OK",
            status=200,
            data={
                "role": _user_role_tag(env) or "tasker",
                "columns": URLS_ADDED_COLUMNS,
                "rows": rows,
                "total_records": total,
                "page": page,
                "limit": limit,
            },
        )

    @http.route(
        "/api/v1/leviathan_ext/leviathan_team_view",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def leviathan_team_view(self, **kwargs):
        guard = _require_leviathan_user()
        if guard is not None:
            return guard

        env = request.env
        params = request.params or {}

        project, error = _resolve_team_project(env, params)
        if error is not None:
            return error

        members = _collect_team_members(project)

        search = (params.get("search") or "").strip().lower()
        if search:
            def _matches(pair):
                emp, _tag = pair
                user = emp.user_id
                name = (emp.name or (user.name if user else "")).lower()
                email = ((user.email if user else "") or emp.work_email or "").lower()
                return search in name or search in email
            members = [m for m in members if _matches(m)]

        role_filter = (params.get("role") or "").strip().lower()
        if role_filter:
            members = [m for m in members if m[1] == role_filter]

        total = len(members)
        page = max(1, _coerce_int(params.get("page"), 1))
        limit = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT)
        limit = max(1, min(limit, LIST_MAX_LIMIT))
        offset = (page - 1) * limit
        page_slice = members[offset:offset + limit]

        rows = [_serialize_team_member(env, emp, tag) for emp, tag in page_slice]
        return return_Response(
            message="OK",
            status=200,
            data={
                "role": _user_role_tag(env) or "tasker",
                "columns": TEAM_VIEW_COLUMNS,
                "rows": rows,
                "total_records": total,
                "page": page,
                "limit": limit,
            },
        )
