import logging

from odoo import http
from odoo.http import request

from .utility import validate_request, validate_token, return_Response

_logger = logging.getLogger(__name__)

RELATIONAL_TYPES = ('many2one', 'one2many', 'many2many')


def _resolve_selection(field):
    sel = field.selection
    if callable(sel):
        try:
            Model = request.env[field.model_name].sudo()
            sel = sel(Model)
        except Exception:
            sel = []
    return [{"value": k, "label": l} for k, l in (sel or [])]


def _serialize_field(field, include_sub=True):
    info = {
        "name": field.name,
        "string": field.string or "",
        "type": field.type,
        # "required": bool(field.required),
        # "readonly": bool(field.readonly),
        # "store": bool(field.store),
        # "help": field.help or "",
    }
    # if field.type == 'selection':
    #     info["selection"] = _resolve_selection(field)
    if field.type in RELATIONAL_TYPES:
        info["comodel_name"] = field.comodel_name or ""
        if field.type == 'one2many':
            info["inverse_name"] = getattr(field, 'inverse_name', '') or ""
        if include_sub and field.comodel_name and field.comodel_name in request.env:
            CoModel = request.env[field.comodel_name].sudo()
            info["sub_fields"] = [
                _serialize_field(f, include_sub=False)
                for f in sorted(CoModel._fields.values(), key=lambda x: x.name)
            ]
        else:
            info["sub_fields"] = []
    return info


class ProjectConnectedFieldsController(http.Controller):

    @validate_token
    @http.route(
        '/api/v1/get_project_connected_table_fields',
        methods=['GET'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_request({"project_id": {"type": "str", "required": True}})
    def get_project_connected_table_fields(self, **kwargs):
        try:
            project_id = int(kwargs.get('project_id'))
        except (TypeError, ValueError):
            return return_Response(
                message="Invalid project_id, must be an integer.",
                status=400,
            )

        Project = request.env['project.project'].sudo()
        project = Project.browse(project_id)
        if not project.exists():
            return return_Response(
                message=f"Project {project_id} not found.",
                status=404,
            )

        connected_table = (getattr(project, 'connected_table', None) or '').strip()
        if not connected_table:
            return return_Response(
                message="Project has no connected_table value.",
                status=400,
            )

        if connected_table not in request.env:
            return return_Response(
                message=f"Model '{connected_table}' is not registered.",
                status=404,
            )

        Model = request.env[connected_table].sudo()
        try:
            fields_list = [
                _serialize_field(f)
                for f in sorted(Model._fields.values(), key=lambda x: x.name)
            ]
        except Exception as e:
            _logger.exception("get_project_connected_table_fields failed")
            return return_Response(message=str(e), status=400)

        return return_Response(
            message="OK",
            status=200,
            data={
                "project_id": project.id,
                "project_name": project.name or "",
                "connected_table": connected_table,
                "fields": fields_list,
                "total": len(fields_list),
            },
        )
