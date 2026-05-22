from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _scope, _user_role_tag

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _serialize_category(category):
    return {
        "id": category.id,
        "name": category.name or "",
        "code": category.code or "",
        "active": category.active,
    }


def _serialize_tasker(user):
    return {
        "id": user.id,
        "name": user.name or "",
        "login": user.login or "",
        "email": user.email or "",
        "active": user.active,
    }


class GohanReferenceDataController(http.Controller):

    @http.route(
        "/api/v1/gohan_ext/categories",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def gohan_ext_list_categories(self, **kwargs):
        """List gohan.job website categories."""
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Gohan data.",
                status=403,
            )

        params = request.params or {}
        active_raw = str(params.get("active", "true")).strip().lower()
        active_filter = active_raw not in ("false", "0", "no")
        search = (params.get("search") or "").strip()
        limit = max(
            1, min(_coerce_int(params.get("limit"), DEFAULT_LIMIT), MAX_LIMIT)
        )
        offset = max(0, _coerce_int(params.get("offset"), 0))

        domain = []
        if search:
            domain = ["|", ("name", "ilike", search), ("code", "ilike", search)]

        Category = env["gohan.category"].sudo()
        if not active_filter:
            Category = Category.with_context(active_test=False)

        total = Category.search_count(domain)
        records = Category.search(domain, limit=limit, offset=offset)

        return return_Response(
            message="OK",
            status=200,
            data={
                "categories": [_serialize_category(c) for c in records],
                "count": len(records),
                "total": total,
                "offset": offset,
                "limit": limit,
            },
        )

    @http.route(
        "/api/v1/gohan_ext/taskers",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def gohan_ext_list_taskers(self, **kwargs):
        """List taskers visible to the calling user, scoped by project."""
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Gohan data.",
                status=403,
            )

        params = request.params or {}
        search = (params.get("search") or "").strip()
        limit = max(
            1, min(_coerce_int(params.get("limit"), DEFAULT_LIMIT), MAX_LIMIT)
        )
        offset = max(0, _coerce_int(params.get("offset"), 0))

        tag, job_domain, projects = _scope(env)
        if tag == "tasker":
            tasker_user_ids = [env.user.id]
        else:
            tasker_user_ids = projects.mapped("project_tasker").mapped(
                "user_id"
            ).ids

        domain = [("id", "in", tasker_user_ids)]
        if search:
            domain += [
                "|", "|",
                ("name", "ilike", search),
                ("login", "ilike", search),
                ("email", "ilike", search),
            ]

        Users = env["res.users"].sudo()
        total = Users.search_count(domain)
        records = Users.search(
            domain, limit=limit, offset=offset, order="name asc, id asc"
        )

        return return_Response(
            message="OK",
            status=200,
            data={
                "taskers": [_serialize_tasker(u) for u in records],
                "count": len(records),
                "total": total,
                "offset": offset,
                "limit": limit,
            },
        )
