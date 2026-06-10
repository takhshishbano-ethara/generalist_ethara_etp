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


def _serialize_tasker(user):
    return {
        "id": user.id,
        "name": user.name or "",
        "login": user.login or "",
        "email": user.email or "",
        "active": user.active,
    }


class TalosReferenceDataController(http.Controller):

    @http.route(
        "/api/v1/talos_ext/categories",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def talos_ext_list_categories(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Talos data.",
                status=403,
            )

        params = request.params or {}
        search = (params.get("search") or "").strip().lower()
        limit = max(
            1, min(_coerce_int(params.get("limit"), DEFAULT_LIMIT), MAX_LIMIT)
        )
        offset = max(0, _coerce_int(params.get("offset"), 0))

        Talos = env["talos.talos"]
        selection = list(Talos._fields["task_type"].selection)
        if search:
            selection = [
                (key, label)
                for key, label in selection
                if search in (key or "").lower() or search in (label or "").lower()
            ]
        total = len(selection)
        page = selection[offset:offset + limit]
        categories = [
            {
                "id": key,
                "name": label,
                "technical_key": key,
                "active": True,
            }
            for key, label in page
        ]
        return return_Response(
            message="OK",
            status=200,
            data={
                "categories": categories,
                "count": len(categories),
                "total": total,
                "offset": offset,
                "limit": limit,
            },
        )

    @http.route(
        "/api/v1/talos_ext/taskers",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def talos_ext_list_taskers(self, **kwargs):
        env = request.env
        tag = _user_role_tag(env)
        if tag is None:
            return return_Response(
                message="You are not allowed to access Talos data.",
                status=403,
            )

        params = request.params or {}
        search = (params.get("search") or "").strip()
        limit = max(
            1, min(_coerce_int(params.get("limit"), DEFAULT_LIMIT), MAX_LIMIT)
        )
        offset = max(0, _coerce_int(params.get("offset"), 0))

        if tag == "tasker":
            tasker_user_ids = [env.user.id]
        else:
            tasker_group = env.ref(
                "etp_user_roles.group_tasker", raise_if_not_found=False
            )
            tasker_user_ids = tasker_group.users.ids if tasker_group else []

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
