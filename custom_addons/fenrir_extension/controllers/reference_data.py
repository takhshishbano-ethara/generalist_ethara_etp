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
        result = int(value)
        return result if result >= 0 else default
    except (TypeError, ValueError):
        return default


def _serialize_tasker(user):
    return {
        "id": user.id,
        "name": user.name or "",
        "login": user.login or "",
        "email": user.email or "",
        "active": bool(user.active),
    }


def _serialize_category(category):
    return {
        "id": category.id,
        "name": category.name or "",
        "technical_key": category.code or "",
        "active": bool(category.active),
    }


class FenrirReferenceDataController(http.Controller):

    @http.route(
        "/api/v1/fenrir_ext/categories",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def fenrir_ext_list_categories(self, **kwargs):
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to list categories.",
                status=403,
            )

        params = kwargs or {}
        offset = _coerce_int(params.get("offset"), 0)
        limit = _coerce_int(params.get("limit"), DEFAULT_LIMIT) or DEFAULT_LIMIT
        if limit > MAX_LIMIT:
            limit = MAX_LIMIT

        Category = env["fenrir.category"].sudo()
        domain = [("active", "=", True)]
        search = (params.get("search") or "").strip()
        if search:
            domain += [
                "|",
                ("name", "ilike", search),
                ("code", "ilike", search),
            ]

        total = Category.search_count(domain)
        records = Category.search(
            domain, limit=limit, offset=offset, order="sequence asc, name asc"
        )
        rows = [_serialize_category(c) for c in records]

        data = {
            "categories": rows,
            "count": len(rows),
            "total": total,
            "offset": offset,
            "limit": limit,
        }
        return return_Response(
            message="Categories fetched successfully.",
            status=200,
            data=data,
        )

    @http.route(
        "/api/v1/fenrir_ext/taskers",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def fenrir_ext_list_taskers(self, **kwargs):
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to list taskers.",
                status=403,
            )

        params = kwargs or {}
        offset = _coerce_int(params.get("offset"), 0)
        limit = _coerce_int(params.get("limit"), DEFAULT_LIMIT) or DEFAULT_LIMIT
        if limit > MAX_LIMIT:
            limit = MAX_LIMIT

        tag, _scope_domain, tasks = _scope(env)
        if tag == "tasker":
            tasker_user_ids = [env.user.id]
        else:
            tasker_user_ids = list({uid for uid in tasks.mapped("lead_user_id").ids if uid})

        domain = [("id", "in", tasker_user_ids)]

        search = (params.get("search") or "").strip()
        if search:
            domain += [
                "|",
                "|",
                ("name", "ilike", search),
                ("login", "ilike", search),
                ("email", "ilike", search),
            ]

        Users = env["res.users"].sudo()
        total = Users.search_count(domain)
        records = Users.search(
            domain,
            limit=limit,
            offset=offset,
            order="name asc, id asc",
        )
        rows = [_serialize_tasker(user) for user in records]

        data = {
            "taskers": rows,
            "count": len(rows),
            "total": total,
            "offset": offset,
            "limit": limit,
        }
        return return_Response(
            message="Taskers fetched successfully.",
            status=200,
            data=data,
        )
