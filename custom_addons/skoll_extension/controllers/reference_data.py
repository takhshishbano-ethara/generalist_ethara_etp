from __future__ import annotations

import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _scope, _user_role_tag

_logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _resolve_pagination(params):
    raw_limit = params.get("limit") or DEFAULT_LIMIT
    raw_offset = params.get("offset") or 0
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(raw_offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    return limit, offset


class SkollReferenceDataController(http.Controller):
    @http.route(
        "/api/v1/skoll_ext/categories",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def categories(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="Forbidden",
                status=403,
                errors=["User has no Skoll role"],
            )
        try:
            limit, offset = _resolve_pagination(kwargs)
            search = (kwargs.get("search") or "").strip()
            LifeDomain = env["skoll.tag.life_domain"].sudo()
            dom = []
            if search:
                dom.append(("name", "ilike", search))
            total = LifeDomain.search_count(dom)
            records = LifeDomain.search(dom, limit=limit, offset=offset, order="name asc")
            categories = [
                {
                    "id": rec.id,
                    "name": rec.name or "",
                    "technical_key": rec.name or "",
                    "active": True,
                }
                for rec in records
            ]
        except Exception as exc:
            _logger.exception("Skoll categories endpoint failed")
            return return_Response(
                message="Internal Server Error",
                status=500,
                errors=[str(exc)],
            )
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
        "/api/v1/skoll_ext/taskers",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def taskers(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="Forbidden",
                status=403,
                errors=["User has no Skoll role"],
            )
        try:
            limit, offset = _resolve_pagination(kwargs)
            search = (kwargs.get("search") or "").strip()

            _tag, task_domain, _personas = _scope(env)
            tasks = env["skoll.skoll"].sudo().search(task_domain)
            user_ids = set()
            for task in tasks:
                if task.employee_id and task.employee_id.user_id:
                    user_ids.add(task.employee_id.user_id.id)

            Users = env["res.users"].sudo()
            user_dom = [("id", "in", list(user_ids))]
            if search:
                user_dom += ["|", ("name", "ilike", search), ("login", "ilike", search)]
            total = Users.search_count(user_dom)
            users = Users.search(user_dom, limit=limit, offset=offset, order="name asc")
            taskers = [
                {
                    "id": user.id,
                    "name": user.name or "",
                    "login": user.login or "",
                    "email": user.email or "",
                    "active": bool(user.active),
                }
                for user in users
            ]
        except Exception as exc:
            _logger.exception("Skoll taskers endpoint failed")
            return return_Response(
                message="Internal Server Error",
                status=500,
                errors=[str(exc)],
            )
        return return_Response(
            message="OK",
            status=200,
            data={
                "taskers": taskers,
                "count": len(taskers),
                "total": total,
                "offset": offset,
                "limit": limit,
            },
        )
