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


class CrowleySourcingReferenceDataController(http.Controller):

    @http.route(
        "/api/v1/crowley_sourcing_ext/categories",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def crowley_sourcing_ext_list_categories(self, **kwargs):
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

        selection = list(
            env["video.editor.project"]._fields["category"].selection
        )

        search = (params.get("search") or "").strip().lower()
        if search:
            selection = [
                (slug, label)
                for slug, label in selection
                if search in slug.lower() or search in (label or "").lower()
            ]

        total = len(selection)
        window = selection[offset : offset + limit]
        rows = [
            {
                "id": slug,
                "name": label,
                "technical_key": slug,
                "active": True,
            }
            for slug, label in window
        ]

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
        "/api/v1/crowley_sourcing_ext/taskers",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def crowley_sourcing_ext_list_taskers(self, **kwargs):
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

        tag, _gen_domain, projects = _scope(env)
        if tag == "tasker":
            tasker_user_ids = [env.user.id]
        else:
            tasker_user_ids = (
                projects.mapped("project_tasker")
                .filtered("task_forge_active")
                .mapped("user_id")
                .ids
            )

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
