from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .main import (
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    _coerce_int,
    _iso,
    _job_scope_domain,
    _require_leviathan_user,
)


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


def _build_task_view_domain(env, params):
    domain = []

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

    urls_added = (params.get("urls_added") or "").strip()
    if urls_added in [1, '1']:
        domain.append(("url", "!=", False))

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


def _serialize_task(job, base_url):
    asset_keys = job.asset_keys or []
    asset_links = [f"{base_url}/{key}" for key in asset_keys] if base_url else []
    return {
        "id": job.id,
        "seq": job.name or "",
        "name": job.site_name or job.url or "",
        "category": job.category_id.name or "",
        "score": job.score or 0.0,
        "assets_count": len(asset_keys),
        "asset_file_links": asset_links,
        "assigned_ql_name": job.user_id.employee_id.task_forge_qr_id.name or "",
        "assigned_tasker_name": job.user_id.name or "",
        "source": job.url or "",
        "created_date": _iso(job.create_date),
        "created_by": job.create_uid.name or "",
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
                "tasks": tasks,
                "total_records": total,
                "page": page,
                "limit": limit,
            },
        )
