from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _scope, _user_role_tag


DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _coerce_int(raw, default):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _serialize_domain(domain):
    return {
        "id": domain.id,
        "name": domain.name or "",
        "parent_id": domain.parent_id.id if domain.parent_id else 0,
        "parent_name": domain.parent_id.name if domain.parent_id else "",
        "child_count": len(domain.child_ids),
    }


def _serialize_persona(persona):
    return {
        "id": persona.id,
        "name": persona.name or "",
        "active": persona.active,
        "is_admin": getattr(persona, "is_kensei2_admin", False),
        "task_count": getattr(persona, "task_count", 0),
    }


def _serialize_tasker(user):
    return {
        "id": user.id,
        "name": user.name or "",
        "login": user.login or "",
        "email": user.partner_id.email or "" if user.partner_id else "",
        "active": user.active,
    }


class KenseiReferenceDataController(http.Controller):

    @http.route(
        "/api/v1/kensei_ext/domains",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def kensei_ext_domains(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Kensei domains.",
                status=403,
                errors=["forbidden"],
            )
        params = request.params
        domain = []
        parent_raw = params.get("parent_id")
        if parent_raw:
            parent_id = _coerce_int(parent_raw, None)
            if parent_id is None:
                return return_Response(
                    message="Invalid parent_id.",
                    status=400,
                    errors=["invalid_parent_id"],
                )
            domain.append(("parent_id", "=", parent_id))
        search = params.get("search")
        if search:
            domain.append(("name", "ilike", search))
        limit = min(_coerce_int(params.get("limit"), DEFAULT_LIMIT), MAX_LIMIT)
        if limit < 1:
            limit = DEFAULT_LIMIT
        offset = max(_coerce_int(params.get("offset"), 0), 0)
        model = env["kensei2.domain"].sudo()
        total = model.search_count(domain)
        records = model.search(domain, limit=limit, offset=offset, order="name asc")
        payload = {
            "domains": [_serialize_domain(r) for r in records],
            "count": len(records),
            "total": total,
            "offset": offset,
            "limit": limit,
        }
        return return_Response(message="Success", status=200, data=payload)

    @http.route(
        "/api/v1/kensei_ext/personas",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def kensei_ext_personas(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Kensei personas.",
                status=403,
                errors=["forbidden"],
            )
        params = request.params
        domain = []
        active_raw = params.get("active")
        if active_raw is not None and active_raw != "":
            if active_raw in ("true", "1", "True"):
                domain.append(("active", "=", True))
            elif active_raw in ("false", "0", "False"):
                domain.append(("active", "=", False))
        search = params.get("search")
        if search:
            domain.append(("name", "ilike", search))
        limit = min(_coerce_int(params.get("limit"), DEFAULT_LIMIT), MAX_LIMIT)
        if limit < 1:
            limit = DEFAULT_LIMIT
        offset = max(_coerce_int(params.get("offset"), 0), 0)
        model = env["kensei2.persona"].sudo()
        total = model.search_count(domain)
        records = model.search(domain, limit=limit, offset=offset, order="name asc")
        payload = {
            "personas": [_serialize_persona(r) for r in records],
            "count": len(records),
            "total": total,
            "offset": offset,
            "limit": limit,
        }
        return return_Response(message="Success", status=200, data=payload)

    @http.route(
        "/api/v1/kensei_ext/taskers",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def kensei_ext_taskers(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Kensei taskers.",
                status=403,
                errors=["forbidden"],
            )
        params = request.params
        _tag, _scope_domain, projects = _scope(env)
        tasker_user_ids = projects.mapped("project_tasker").mapped("user_id").ids
        if not tasker_user_ids:
            return return_Response(
                message="Success",
                status=200,
                data={
                    "taskers": [],
                    "count": 0,
                    "total": 0,
                    "offset": 0,
                    "limit": DEFAULT_LIMIT,
                },
            )
        domain = [("id", "in", tasker_user_ids)]
        search = params.get("search")
        if search:
            domain.append(("name", "ilike", search))
        active_raw = params.get("active")
        if active_raw in ("true", "1", "True"):
            domain.append(("active", "=", True))
        elif active_raw in ("false", "0", "False"):
            domain.append(("active", "=", False))
        limit = min(_coerce_int(params.get("limit"), DEFAULT_LIMIT), MAX_LIMIT)
        if limit < 1:
            limit = DEFAULT_LIMIT
        offset = max(_coerce_int(params.get("offset"), 0), 0)
        model = env["res.users"].sudo()
        total = model.search_count(domain)
        records = model.search(domain, limit=limit, offset=offset, order="name asc")
        payload = {
            "taskers": [_serialize_tasker(r) for r in records],
            "count": len(records),
            "total": total,
            "offset": offset,
            "limit": limit,
        }
        return return_Response(message="Success", status=200, data=payload)
