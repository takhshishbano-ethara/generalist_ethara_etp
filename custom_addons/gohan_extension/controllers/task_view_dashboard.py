from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    _create_date_domain,
    _domain_from_url,
    _format_short_date,
    _parse_date,
    _scope,
    _state_badge,
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

# Self-describing columns for the Tasks table. The shape mirrors the
# leviathan_extension task view (the schema the Flutter project_detail Tasks
# tab is built against): every display cell is a plain string/date, with the
# rich values kept in sibling raw fields on each row. Keeping the key set and
# `type` identical to leviathan is what lets the shared Flutter table render
# gohan tasks without per-project special-casing.
TASK_VIEW_COLUMNS = [
    {"key": "task", "label": "Task", "type": "string", "width": "fill"},
    {"key": "category", "label": "Category", "type": "string", "width": 180},
    {"key": "status", "label": "Status", "type": "string", "width": 150},
    {"key": "qc", "label": "QC", "type": "string", "width": 120},
    {"key": "assets", "label": "Assets", "type": "string", "width": 90},
    {"key": "assigned_ql", "label": "Assigned QL", "type": "string", "width": 150},
    {"key": "created", "label": "Created", "type": "date", "width": 90},
]

# gohan.job.qc_verdict -> short label for the flat "QC" column ("Pass · 84").
# Mirrors leviathan's Pass/Warning/Fail vocabulary so the column reads the same.
QC_VERDICT_LABEL = {
    "shippable": "Pass",
    "fixes": "Warning",
    "not_shippable": "Fail",
}


def _s3_base_url(env):
    """Resolve the gohan S3/CDN base used to turn asset_keys into full URLs."""
    icp = env["ir.config_parameter"].sudo()
    cdn_url = icp.get_param("gohan.s3_cdn_url", "")
    bucket = icp.get_param("gohan.s3_bucket", "")
    region = icp.get_param("gohan.s3_region", "us-east-1")
    if cdn_url:
        return cdn_url.rstrip("/")
    if bucket:
        return f"https://{bucket}.s3.{region}.amazonaws.com"
    return ""


def _qc_string(qc_verdict, score):
    """Flat "QC" cell: "—" when undecided, else "Pass · 84"-style label."""
    if not qc_verdict:
        return "—"
    prefix = QC_VERDICT_LABEL.get(qc_verdict, qc_verdict)
    score_int = int(round(score or 0))
    return f"{prefix} · {score_int}" if score else prefix


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
    Job = env["gohan.job"]

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

    raw_eq_tier = (params.get("eq_tier") or "").strip()
    if raw_eq_tier:
        values, error = _parse_selection(Job, "eq_tier", raw_eq_tier, "eq_tier")
        if error is not None:
            return None, error
        if values:
            domain.append(("eq_tier", "in", values))

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


def _serialize(job, base_url):
    """One Tasks-table row in the leviathan/Flutter shape: every display column
    (task/category/status/qc/assets/assigned_ql/created) is a flat string/date,
    followed by the raw fields the client keeps for filter/sort and asset
    previews. Key order and types mirror leviathan_extension's task view so the
    shared Flutter table renders gohan tasks unchanged."""
    url = job.url or ""
    seq = job.name or ""
    title = job.site_name or _domain_from_url(url) or seq or ""

    asset_keys = job.asset_keys or []
    asset_links = [f"{base_url}/{key}" for key in asset_keys] if base_url else []
    assets_count = len(asset_keys)

    qr_name = ""
    if job.user_id and job.user_id.employee_id:
        qr_name = job.user_id.employee_id.task_forge_qr_id.name or ""

    return {
        "id": job.id,
        "task": f"{title} — {seq}" if title and seq else (title or seq),
        "category": job.category_id.name or "",
        "status": _state_badge(job.state)["label"],
        "qc": _qc_string(job.qc_verdict, job.score),
        "assets": f"{assets_count} files" if assets_count else "—",
        "assigned_ql": qr_name or "Unassigned",
        "created": _format_short_date(job.create_date),
        # Raw fields retained for client-side filtering / sorting / previews.
        "seq": seq,
        "state": job.state or "",
        "qc_verdict": job.qc_verdict or "",
        "score": job.score or 0.0,
        "category_id": job.category_id.id or False,
        "assets_count": assets_count,
        "asset_file_links": asset_links,
        "assigned_tasker_name": job.user_id.name or "",
        "source": url,
        "created_at": job.create_date.isoformat() if job.create_date else "",
        "created_by": job.create_uid.name or "",
    }


class GohanTaskViewDashboardController(http.Controller):

    @http.route(
        "/api/v1/gohan_ext/task_view_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def gohan_ext_task_view_dashboard(self, **kwargs):
        """Paginated, filterable, sortable task listing for gohan.job."""
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Gohan task view.",
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

        Job = env["gohan.job"].sudo()
        total = Job.search_count(domain)
        records = Job.search(domain, limit=limit, offset=offset, order=order)
        base_url = _s3_base_url(env)
        tasks = [_serialize(job, base_url) for job in records]
        # Flat pagination (total_records/page/limit at the data root), matching
        # the leviathan task view the Flutter client parses. A nested
        # `pagination` object would leave those fields unread (client defaults).
        data = {
            "role": _user_role_tag(env) or "tasker",
            "columns": TASK_VIEW_COLUMNS,
            "rows": tasks,
            "total_records": total,
            "page": page,
            "limit": limit,
        }
        return return_Response(message="OK", status=200, data=data)
