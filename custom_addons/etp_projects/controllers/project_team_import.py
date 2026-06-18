import base64
import logging

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)
from odoo.addons.etp_projects.models.project_team_csv_import import FIELD_LABELS

_logger = logging.getLogger(__name__)

TRUE_VALUES = {'1', 'true', 'yes', 'on', 'y', 't'}


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in TRUE_VALUES


class ProjectTeamImportController(http.Controller):

    @http.route(
        '/api/v1/etp_projects/team/import',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def import_project_team(self, project_id=None, replace_existing=None, **params):
        try:
            upload = request.httprequest.files.get('file')
            if not upload:
                return return_Response(
                    message="A team file (CSV or Excel) is required. Send it as "
                            "multipart/form-data with field name 'file'.",
                    status=400,
                )

            if not project_id:
                return return_Response(
                    message="'project_id' is required.", status=400,
                )
            try:
                project_id_int = int(project_id)
            except (TypeError, ValueError):
                return return_Response(
                    message="'project_id' must be an integer.", status=400,
                )

            raw_bytes = upload.read()
            if not raw_bytes:
                return return_Response(message="Uploaded file is empty.", status=400)

            filename = upload.filename or 'team.csv'
            file_b64 = base64.b64encode(raw_bytes).decode('ascii')
            replace_flag = _to_bool(replace_existing)

            project = request.env['project.project'].sudo().browse(project_id_int)
            if not project.exists():
                return return_Response(
                    message="Project with id %s not found." % project_id_int,
                    status=404,
                )

            wizard = request.env['etp.project.team.csv.import'].sudo().create({
                'project_id': project.id,
                'csv_file': file_b64,
                'csv_filename': filename,
                'replace_existing': replace_flag,
            })

            try:
                rows = wizard._read_csv()
                per_field_ids, issues = wizard._resolve_rows(rows)
                write_vals = wizard._build_write_vals(per_field_ids)
            except UserError as ue:
                return return_Response(message=str(ue), status=400)

            if write_vals:
                project.write(write_vals)

            data = {
                "project_id": project.id,
                "project_name": project.name or "",
                "filename": filename,
                "replace_existing": replace_flag,
                "team_counts": {
                    FIELD_LABELS[field]: len(per_field_ids.get(field, set()))
                    for field in FIELD_LABELS
                },
                "team_member_ids": {
                    FIELD_LABELS[field]: sorted(list(per_field_ids.get(field, set())))
                    for field in FIELD_LABELS
                },
                "issues": issues,
                "applied_team": {
                    "tasker_ids": project.project_tasker.ids,
                    "qr_ids": project.project_qc_reviewer.ids,
                    "pl_ids": project.project_lead.ids,
                    "tpm_ids": project.project_tpm.ids,
                },
            }
            return return_Response(
                message="Project team updated successfully.",
                status=200,
                data={"data": data},
            )
        except Exception as e:
            _logger.exception("import_project_team failed")
            return return_Response(
                message="Something went wrong.",
                status=400,
                errors=[str(e)],
            )
