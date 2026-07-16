import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    safe_get_value,
    validate_request,
    validate_token,
)

from ..models.role_map import VALID_ROLE_KEYS, resolve_role_ids
from ..models.ethara_project import VALID_PROJECT_STATES
from ..models.hr_employee import WORK_STATUS_ALLOCATED, WORK_STATUS_UNALLOCATED

_logger = logging.getLogger(__name__)


TPM_ROLE_KEY = 'tpm'
PL_QL_ROLE_KEY = 'pl_ql'
RND_ROLE_KEY = 'rnd'


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _split_ids(raw):
    if raw is None:
        return None
    if isinstance(raw, list):
        cleaned = []
        for v in raw:
            n = _to_int(v)
            if n is not None:
                cleaned.append(n)
        return cleaned
    if isinstance(raw, (int, str)):
        n = _to_int(raw)
        return [n] if n is not None else []
    return []


def _validate_role_membership(employee_ids, role_key, label):
    if not employee_ids:
        return []
    allowed_role_ids = set(resolve_role_ids(request.env, role_key))
    if not allowed_role_ids:
        return [
            f"{label}: no api.role records resolved for '{role_key}'. "
            'Check api_auth_gateway seed data.'
        ]
    Employee = request.env['hr.employee'].sudo()
    employees = Employee.browse(employee_ids).exists()
    missing = set(employee_ids) - set(employees.ids)
    errors = []
    for missing_id in missing:
        errors.append(f"{label}: employee id {missing_id} does not exist")
    for emp in employees:
        role_id = (
            emp.user_id.user_role.id
            if emp.user_id and emp.user_id.user_role
            else False
        )
        if not role_id or role_id not in allowed_role_ids:
            role_name = (
                emp.user_id.user_role.name
                if emp.user_id and emp.user_id.user_role
                else 'None'
            )
            errors.append(
                f"{label}: employee {emp.name} (id={emp.id}) has role "
                f"'{role_name}' which is not permitted for role_key='{role_key}'."
            )
    return errors


def _emp_full(emp):
    user = emp.user_id
    role = user.user_role if user and user.user_role else False
    return {
        'employee_id': emp.id,
        'employee_name': emp.name or '',
        'work_email': emp.work_email or '',
        'work_phone': emp.work_phone or '',
        'mobile_phone': emp.mobile_phone or '',
        'job_title': emp.job_title or '',
        'job_id': emp.job_id.id if emp.job_id else False,
        'job_name': emp.job_id.name if emp.job_id else '',
        'department_id': emp.department_id.id if emp.department_id else False,
        'department_name': emp.department_id.name if emp.department_id else '',
        'user_id': user.id if user else False,
        'user_name': user.name if user else '',
        'user_login': user.login if user else '',
        'user_email': user.email if user else '',
        'user_phone': user.phone if user else '',
        'role_id': role.id if role else False,
        'role_name': role.name if role else '',
        'role_user_type': role.user_type if role else '',
        'work_status': emp.work_status or WORK_STATUS_UNALLOCATED,
    }


def _attachment_brief(att):
    return {
        'id': att.id,
        'name': att.name or '',
        'attachment_url': att.attachment_url or '',
        'file_name': att.file_name or '',
    }


def _serialize_project(project, detail=False):
    data = {
        'id': project.id,
        'name': project.name or '',
        'client_name': project.client_name or '',
        'internal_project_name': project.internal_project_name or '',
        'project_goal': project.project_goal or '',
        'start_date': safe_get_value(project, 'start_date', 'date'),
        'end_date': safe_get_value(project, 'end_date', 'date'),
        'state': project.state or '',
        'attachment_count': project.attachment_count,
    }
    if detail:
        data['assigned_tpm'] = [_emp_full(e) for e in project.assigned_tpm_ids]
        data['assigned_pl_ql'] = [_emp_full(e) for e in project.assigned_pl_ql_ids]
        data['assigned_rnd'] = [_emp_full(e) for e in project.assigned_rnd_ids]
        data['attachments'] = [_attachment_brief(a) for a in project.attachment_ids]
    return data


def _build_attachment_vals(attachments_raw):
    commands = []
    errors = []
    if not isinstance(attachments_raw, list):
        errors.append("'attachments' must be a list of objects.")
        return commands, errors

    for idx, item in enumerate(attachments_raw):
        if not isinstance(item, dict):
            errors.append(f"attachments[{idx}] must be an object.")
            continue
        name = (item.get('name') or '').strip()
        url = (item.get('attachment_url') or '').strip()
        file_b64 = item.get('file_base64')
        file_name = (item.get('file_name') or '').strip()

        if not name and not file_name and not url:
            errors.append(
                f"attachments[{idx}]: at least 'name', 'file_name' or "
                "'attachment_url' is required."
            )
            continue

        vals = {}
        if name:
            vals['name'] = name
        elif file_name:
            vals['name'] = file_name
        else:
            vals['name'] = url

        if file_name:
            vals['file_name'] = file_name

        if file_b64:
            vals['file_upload'] = file_b64
            vals['attachment_url'] = url or 'pending-s3-upload'
        elif url:
            vals['attachment_url'] = url
        else:
            errors.append(
                f"attachments[{idx}]: provide 'attachment_url' or "
                "'file_base64' + 'file_name'."
            )
            continue

        commands.append((0, 0, vals))
    return commands, errors


class EtharaProjectController(http.Controller):

    @http.route(
        '/api/v1/ethara_project/create',
        methods=['POST'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_token
    @validate_request({
        'name': {'type': 'string', 'required': True},
        'client_name': {'type': 'string', 'required': True},
    })
    def create_project(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}

            tpm_ids = _split_ids(jdata.get('assigned_tpm_ids')) or []
            pl_ql_ids = _split_ids(jdata.get('assigned_pl_ql_ids')) or []
            rnd_ids = _split_ids(jdata.get('assigned_rnd_ids')) or []

            role_errors = []
            role_errors += _validate_role_membership(tpm_ids, TPM_ROLE_KEY, 'assigned_tpm_ids')
            role_errors += _validate_role_membership(pl_ql_ids, PL_QL_ROLE_KEY, 'assigned_pl_ql_ids')
            role_errors += _validate_role_membership(rnd_ids, RND_ROLE_KEY, 'assigned_rnd_ids')
            if role_errors:
                return return_Response(
                    message='Team assignment role check failed.',
                    status=400,
                    errors=role_errors,
                )

            attachment_commands = []
            if 'attachments' in jdata:
                attachment_commands, att_errors = _build_attachment_vals(jdata.get('attachments'))
                if att_errors:
                    return return_Response(
                        message='Invalid attachments payload.',
                        status=400,
                        errors=att_errors,
                    )

            vals = {
                'name': (jdata.get('name') or '').strip(),
                'client_name': (jdata.get('client_name') or '').strip(),
                'internal_project_name': (jdata.get('internal_project_name') or '').strip(),
                'project_goal': (jdata.get('project_goal') or '').strip(),
                'start_date': jdata.get('start_date') or False,
                'end_date': jdata.get('end_date') or False,
                'assigned_tpm_ids': [(6, 0, tpm_ids)],
                'assigned_pl_ql_ids': [(6, 0, pl_ql_ids)],
                'assigned_rnd_ids': [(6, 0, rnd_ids)],
            }
            if attachment_commands:
                vals['attachment_ids'] = attachment_commands

            project = request.env['ethara.project'].sudo().create(vals)
            return return_Response(
                message='Ethara project created.',
                status=200,
                data={'data': _serialize_project(project, detail=True)},
            )
        except Exception as e:
            _logger.exception('ethara_project create failed')
            return return_Response(
                message='Failed to create Ethara project.',
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/ethara_project/update',
        methods=['POST'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_token
    @validate_request({
        'id': {'type': 'int', 'required': True},
    })
    def update_project(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}
            project_id = _to_int(jdata.get('id'))
            if not project_id:
                return return_Response(message="'id' must be an integer.", status=400)

            project = request.env['ethara.project'].sudo().browse(project_id).exists()
            if not project:
                return return_Response(message='Ethara project not found.', status=404)

            vals = {}
            for field in ('name', 'client_name', 'internal_project_name', 'project_goal'):
                if field in jdata:
                    vals[field] = (jdata.get(field) or '').strip()
            for field in ('start_date', 'end_date'):
                if field in jdata:
                    vals[field] = jdata.get(field) or False

            for payload_key, model_field, role_key in (
                ('assigned_tpm_ids', 'assigned_tpm_ids', TPM_ROLE_KEY),
                ('assigned_pl_ql_ids', 'assigned_pl_ql_ids', PL_QL_ROLE_KEY),
                ('assigned_rnd_ids', 'assigned_rnd_ids', RND_ROLE_KEY),
            ):
                if payload_key in jdata:
                    ids = _split_ids(jdata.get(payload_key)) or []
                    errs = _validate_role_membership(ids, role_key, payload_key)
                    if errs:
                        return return_Response(
                            message='Team assignment role check failed.',
                            status=400,
                            errors=errs,
                        )
                    vals[model_field] = [(6, 0, ids)]

            if 'attachments' in jdata:
                commands, att_errors = _build_attachment_vals(jdata.get('attachments'))
                if att_errors:
                    return return_Response(
                        message='Invalid attachments payload.',
                        status=400,
                        errors=att_errors,
                    )
                unlink_cmds = [(2, att.id, 0) for att in project.attachment_ids]
                vals['attachment_ids'] = unlink_cmds + commands

            if vals:
                project.sudo().write(vals)

            return return_Response(
                message='Ethara project updated.',
                status=200,
                data={'data': _serialize_project(project, detail=True)},
            )
        except Exception as e:
            _logger.exception('ethara_project update failed')
            return return_Response(
                message='Failed to update Ethara project.',
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/ethara_project/list',
        methods=['GET'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_token
    def list_projects(self, **kwargs):
        try:
            search = (kwargs.get('search') or '').strip()
            limit = _to_int(kwargs.get('limit')) or 50
            offset = _to_int(kwargs.get('offset')) or 0
            if limit < 1:
                limit = 50
            if limit > 200:
                limit = 200
            if offset < 0:
                offset = 0

            domain = []
            if search:
                domain = [
                    '|', '|',
                    ('name', 'ilike', search),
                    ('client_name', 'ilike', search),
                    ('internal_project_name', 'ilike', search),
                ]

            Project = request.env['ethara.project'].sudo()
            total = Project.search_count(domain)
            projects = Project.search(domain, limit=limit, offset=offset, order='id desc')
            records = [_serialize_project(p, detail=False) for p in projects]

            return return_Response(
                message='OK',
                status=200,
                data={
                    'data': {
                        'total': total,
                        'limit': limit,
                        'offset': offset,
                        'records': records,
                    }
                },
            )
        except Exception as e:
            _logger.exception('ethara_project list failed')
            return return_Response(
                message='Failed to list Ethara projects.',
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/ethara_project/detail',
        methods=['GET'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_token
    def project_detail(self, **kwargs):
        try:
            project_id = _to_int(kwargs.get('id'))
            if not project_id:
                return return_Response(
                    message="Query param 'id' is required and must be an integer.",
                    status=400,
                )

            project = request.env['ethara.project'].sudo().browse(project_id).exists()
            if not project:
                return return_Response(message='Ethara project not found.', status=404)

            return return_Response(
                message='OK',
                status=200,
                data={'data': _serialize_project(project, detail=True)},
            )
        except Exception as e:
            _logger.exception('ethara_project detail failed')
            return return_Response(
                message='Failed to fetch Ethara project.',
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/ethara_project/employees_by_role',
        methods=['GET'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_token
    def employees_by_role(self, **kwargs):
        try:
            role_key = (kwargs.get('role') or '').strip().lower()
            if not role_key:
                return return_Response(
                    message=(
                        "Query param 'role' is required. "
                        f"Allowed: {', '.join(VALID_ROLE_KEYS)}"
                    ),
                    status=400,
                )
            if role_key not in VALID_ROLE_KEYS:
                return return_Response(
                    message=(
                        f"Invalid 'role' value '{role_key}'. "
                        f"Allowed: {', '.join(VALID_ROLE_KEYS)}"
                    ),
                    status=400,
                )

            role_ids = resolve_role_ids(request.env, role_key)
            if not role_ids:
                return return_Response(
                    message=(
                        f"No api.role records resolved for '{role_key}'. "
                        'Check api_auth_gateway seed data.'
                    ),
                    status=500,
                )

            search_text = (kwargs.get('search') or '').strip()
            limit = _to_int(kwargs.get('limit')) or 100
            offset = _to_int(kwargs.get('offset')) or 0
            if limit < 1:
                limit = 100
            if limit > 500:
                limit = 500
            if offset < 0:
                offset = 0

            work_status_filter = (kwargs.get('work_status') or '').strip().lower()
            if work_status_filter and work_status_filter not in (
                WORK_STATUS_ALLOCATED, WORK_STATUS_UNALLOCATED,
            ):
                return return_Response(
                    message=(
                        f"Invalid 'work_status' value '{work_status_filter}'. "
                        f"Allowed: {WORK_STATUS_ALLOCATED}, {WORK_STATUS_UNALLOCATED}"
                    ),
                    status=400,
                )

            domain = [
                ('active', '=', True),
                ('user_id.user_role', 'in', role_ids),
            ]
            if work_status_filter:
                domain.append(('work_status', '=', work_status_filter))
            if search_text:
                domain += [
                    '|', '|',
                    ('name', 'ilike', search_text),
                    ('work_email', 'ilike', search_text),
                    ('job_title', 'ilike', search_text),
                ]

            Employee = request.env['hr.employee'].sudo()
            total = Employee.search_count(domain)
            employees = Employee.search(domain, limit=limit, offset=offset, order='name')
            records = [_emp_full(e) for e in employees]

            return return_Response(
                message='OK',
                status=200,
                data={
                    'data': {
                        'role': role_key,
                        'work_status': work_status_filter or '',
                        'total': total,
                        'limit': limit,
                        'offset': offset,
                        'records': records,
                    }
                },
            )
        except Exception as e:
            _logger.exception('ethara_project employees_by_role failed')
            return return_Response(
                message='Failed to fetch employees by role.',
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/ethara_project/update_state',
        methods=['POST'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_token
    @validate_request({
        'id': {'type': 'int', 'required': True},
        'state': {'type': 'string', 'required': True},
    })
    def update_state(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}
            project_id = _to_int(jdata.get('id'))
            new_state = (jdata.get('state') or '').strip().lower()

            if not project_id:
                return return_Response(message="'id' must be an integer.", status=400)
            if new_state not in VALID_PROJECT_STATES:
                return return_Response(
                    message=(
                        f"Invalid 'state' value '{new_state}'. "
                        f"Allowed: {', '.join(VALID_PROJECT_STATES)}"
                    ),
                    status=400,
                )

            project = request.env['ethara.project'].sudo().browse(project_id).exists()
            if not project:
                return return_Response(message='Ethara project not found.', status=404)

            project.action_set_state(new_state)
            return return_Response(
                message='Ethara project state updated.',
                status=200,
                data={'data': _serialize_project(project, detail=True)},
            )
        except Exception as e:
            _logger.exception('ethara_project update_state failed')
            return return_Response(
                message='Failed to update Ethara project state.',
                status=400,
                errors=[str(e)],
            )
