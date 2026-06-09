"""Question category CRUD endpoints."""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
    validate_request,
)

from .common import (
    coerce_bool,
    paginate,
    pagination_block,
    parse_json_body,
    require_assessment_manager,
    require_assessment_user,
    resolve_order,
    user_role_tag,
)

CATEGORY_COLUMNS = [
    {"key": "name", "label": "Name", "type": "string"},
    {"key": "sequence", "label": "Sequence", "type": "integer"},
    {"key": "question_count", "label": "Questions", "type": "integer"},
    {"key": "active", "label": "Active", "type": "boolean"},
    {"key": "create_date", "label": "Created", "type": "datetime"},
]

SORT_FIELDS = {
    "name": "name",
    "sequence": "sequence",
    "create_date": "create_date",
}


def _serialize(rec):
    return {
        "id": rec.id,
        "name": rec.name or "",
        "sequence": rec.sequence or 0,
        "active": bool(rec.active),
        "description": rec.description or "",
        "question_count": rec.question_count or 0,
        "create_date": rec.create_date.isoformat() if rec.create_date else None,
        "write_date": rec.write_date.isoformat() if rec.write_date else None,
    }


def _build_domain(params):
    domain = []
    search = (params.get("search") or "").strip()
    if search:
        domain.append(("name", "ilike", search))
    active = coerce_bool(params.get("active"))
    if active is True:
        domain.append(("active", "=", True))
    elif active is False:
        domain.append(("active", "=", False))
    return domain


class EtpAssessmentCategoryController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/categories",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_categories(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        domain = _build_domain(params)
        order, error = resolve_order(params, SORT_FIELDS, "sequence", "asc")
        if error is not None:
            return error

        page, limit, offset = paginate(params)
        Category = env["etp.assessment.category"].sudo()
        total = Category.search_count(domain)
        records = Category.search(domain, limit=limit, offset=offset, order=order)
        rows = [_serialize(r) for r in records]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [{
                    "type": "table",
                    "title": "Categories",
                    "columns": CATEGORY_COLUMNS,
                    "rows": rows,
                    "pagination": pagination_block(total, page, limit),
                }],
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/categories/<int:category_id>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_category(self, category_id, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        category = request.env["etp.assessment.category"].sudo().browse(category_id)
        if not category.exists():
            return return_Response(message="Category not found", status=404)
        return return_Response(
            message="OK", status=200, data={"category": _serialize(category)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/categories",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    @validate_request({
        "name": {"type": "string", "required": True},
    })
    def create_category(self, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        jdata = kwargs.get("jdata") or {}
        vals = {
            "name": (jdata.get("name") or "").strip(),
        }
        if jdata.get("sequence") is not None:
            try:
                vals["sequence"] = int(jdata["sequence"])
            except (TypeError, ValueError):
                pass
        if jdata.get("description") is not None:
            vals["description"] = jdata["description"]
        active = coerce_bool(jdata.get("active"))
        if active is not None:
            vals["active"] = active

        category = request.env["etp.assessment.category"].sudo().create(vals)
        return return_Response(
            message="Category created",
            status=200,
            data={"category": _serialize(category)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/categories/<int:category_id>",
        type="http",
        auth="none",
        methods=["PUT", "PATCH"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def update_category(self, category_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        category = request.env["etp.assessment.category"].sudo().browse(category_id)
        if not category.exists():
            return return_Response(message="Category not found", status=404)

        jdata = parse_json_body()
        vals = {}
        if "name" in jdata and jdata["name"]:
            vals["name"] = str(jdata["name"]).strip()
        if "sequence" in jdata:
            try:
                vals["sequence"] = int(jdata["sequence"])
            except (TypeError, ValueError):
                pass
        if "description" in jdata:
            vals["description"] = jdata["description"]
        if "active" in jdata:
            active = coerce_bool(jdata["active"])
            if active is not None:
                vals["active"] = active

        if vals:
            category.write(vals)

        return return_Response(
            message="Category updated",
            status=200,
            data={"category": _serialize(category)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/categories/<int:category_id>",
        type="http",
        auth="none",
        methods=["DELETE"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def delete_category(self, category_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        category = request.env["etp.assessment.category"].sudo().browse(category_id)
        if not category.exists():
            return return_Response(message="Category not found", status=404)

        if category.question_count:
            return return_Response(
                message=(
                    f"Cannot delete category '{category.name}': "
                    f"{category.question_count} question(s) still attached."
                ),
                status=400,
            )

        category.unlink()
        return return_Response(message="Category deleted", status=200)
