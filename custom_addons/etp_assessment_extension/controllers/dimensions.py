"""Master dimension + option CRUD endpoints."""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
    validate_request,
)

from .common import (
    coerce_bool,
    coerce_int,
    paginate,
    pagination_block,
    parse_json_body,
    require_assessment_manager,
    require_assessment_user,
    resolve_order,
    user_role_tag,
)

DIMENSION_COLUMNS = [
    {"key": "name", "label": "Name", "type": "string"},
    {"key": "sequence", "label": "Sequence", "type": "integer"},
    {"key": "option_count", "label": "Options", "type": "integer"},
    {"key": "active", "label": "Active", "type": "boolean"},
]

SORT_FIELDS = {
    "name": "name",
    "sequence": "sequence",
    "create_date": "create_date",
}


def _serialize_option(opt):
    return {
        "id": opt.id,
        "name": opt.name or "",
        "sequence": opt.sequence or 0,
        "dimension_id": opt.dimension_id.id if opt.dimension_id else 0,
    }


def _serialize_dimension(rec, with_options=True):
    data = {
        "id": rec.id,
        "name": rec.name or "",
        "sequence": rec.sequence or 0,
        "active": bool(rec.active),
        "option_count": rec.option_count or 0,
        "options_display": rec.options_display or "",
    }
    if with_options:
        data["options"] = [
            _serialize_option(o)
            for o in rec.option_ids.sorted("sequence")
        ]
    return data


def _build_dimension_domain(params):
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


class EtpAssessmentDimensionController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/dimensions",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_dimensions(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        domain = _build_dimension_domain(params)
        order, error = resolve_order(params, SORT_FIELDS, "sequence", "asc")
        if error is not None:
            return error

        page, limit, offset = paginate(params)
        Dimension = env["etp.assessment.dimension"].sudo()
        total = Dimension.search_count(domain)
        records = Dimension.search(domain, limit=limit, offset=offset, order=order)
        rows = [_serialize_dimension(r) for r in records]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [{
                    "type": "table",
                    "title": "Dimensions",
                    "columns": DIMENSION_COLUMNS,
                    "rows": rows,
                    "pagination": pagination_block(total, page, limit),
                }],
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/dimensions/<int:dimension_id>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_dimension(self, dimension_id, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        dim = request.env["etp.assessment.dimension"].sudo().browse(dimension_id)
        if not dim.exists():
            return return_Response(message="Dimension not found", status=404)
        return return_Response(
            message="OK", status=200,
            data={"dimension": _serialize_dimension(dim)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/dimensions",
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
    def create_dimension(self, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        jdata = kwargs.get("jdata") or {}
        vals = {
            "name": (jdata.get("name") or "").strip(),
        }
        if jdata.get("sequence") is not None:
            vals["sequence"] = coerce_int(jdata["sequence"], 10)
        active = coerce_bool(jdata.get("active"))
        if active is not None:
            vals["active"] = active

        options = jdata.get("options") or []
        if options and isinstance(options, list):
            opt_lines = []
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                name = (opt.get("name") or "").strip()
                if not name:
                    continue
                opt_lines.append((0, 0, {
                    "name": name,
                    "sequence": coerce_int(opt.get("sequence"), 10),
                }))
            if opt_lines:
                vals["option_ids"] = opt_lines

        dim = request.env["etp.assessment.dimension"].sudo().create(vals)
        return return_Response(
            message="Dimension created",
            status=200,
            data={"dimension": _serialize_dimension(dim)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/dimensions/<int:dimension_id>",
        type="http",
        auth="none",
        methods=["PUT", "PATCH"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def update_dimension(self, dimension_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        dim = request.env["etp.assessment.dimension"].sudo().browse(dimension_id)
        if not dim.exists():
            return return_Response(message="Dimension not found", status=404)

        jdata = parse_json_body()
        vals = {}
        if "name" in jdata and jdata["name"]:
            vals["name"] = str(jdata["name"]).strip()
        if "sequence" in jdata:
            vals["sequence"] = coerce_int(jdata["sequence"], dim.sequence)
        if "active" in jdata:
            active = coerce_bool(jdata["active"])
            if active is not None:
                vals["active"] = active

        if vals:
            dim.write(vals)

        return return_Response(
            message="Dimension updated",
            status=200,
            data={"dimension": _serialize_dimension(dim)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/dimensions/<int:dimension_id>",
        type="http",
        auth="none",
        methods=["DELETE"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def delete_dimension(self, dimension_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        dim = request.env["etp.assessment.dimension"].sudo().browse(dimension_id)
        if not dim.exists():
            return return_Response(message="Dimension not found", status=404)
        try:
            dim.unlink()
        except Exception as exc:
            return return_Response(
                message=f"Cannot delete dimension: {exc}",
                status=400,
            )
        return return_Response(message="Dimension deleted", status=200)


class EtpAssessmentDimensionOptionController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/dimension_options",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    @validate_request({
        "dimension_id": {"type": "int", "required": True},
        "name": {"type": "string", "required": True},
    })
    def create_option(self, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        jdata = kwargs.get("jdata") or {}
        dim = (
            request.env["etp.assessment.dimension"]
            .sudo()
            .browse(int(jdata["dimension_id"]))
        )
        if not dim.exists():
            return return_Response(message="Dimension not found", status=404)

        opt = request.env["etp.assessment.dimension.option"].sudo().create({
            "dimension_id": dim.id,
            "name": str(jdata["name"]).strip(),
            "sequence": coerce_int(jdata.get("sequence"), 10),
        })
        return return_Response(
            message="Option created",
            status=200,
            data={"option": _serialize_option(opt)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/dimension_options/<int:option_id>",
        type="http",
        auth="none",
        methods=["PUT", "PATCH"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def update_option(self, option_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        opt = (
            request.env["etp.assessment.dimension.option"]
            .sudo()
            .browse(option_id)
        )
        if not opt.exists():
            return return_Response(message="Option not found", status=404)

        jdata = parse_json_body()
        vals = {}
        if "name" in jdata and jdata["name"]:
            vals["name"] = str(jdata["name"]).strip()
        if "sequence" in jdata:
            vals["sequence"] = coerce_int(jdata["sequence"], opt.sequence)

        if vals:
            opt.write(vals)
        return return_Response(
            message="Option updated", status=200,
            data={"option": _serialize_option(opt)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/dimension_options/<int:option_id>",
        type="http",
        auth="none",
        methods=["DELETE"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def delete_option(self, option_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        opt = (
            request.env["etp.assessment.dimension.option"]
            .sudo()
            .browse(option_id)
        )
        if not opt.exists():
            return return_Response(message="Option not found", status=404)
        try:
            opt.unlink()
        except Exception as exc:
            return return_Response(
                message=f"Cannot delete option: {exc}",
                status=400,
            )
        return return_Response(message="Option deleted", status=200)
