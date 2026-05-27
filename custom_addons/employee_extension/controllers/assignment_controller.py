from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request,
)
import logging

_logger = logging.getLogger(__name__)


CTO_ROLE_REFS = ['api_auth_gateway.role_cto_technical']
TPM_ROLE_REFS = ['api_auth_gateway.role_tpm_technical']
PL_ROLE_REFS = [
    'api_auth_gateway.role_pl_technical',
    'api_auth_gateway.role_pl_stem',
    'api_auth_gateway.role_pl_non_stem',
]
QR_ROLE_REFS = [
    'api_auth_gateway.role_qc_technical',
    'api_auth_gateway.role_qc_stem',
    'api_auth_gateway.role_qc_non_stem',
]
TASKER_ROLE_REFS = [
    'api_auth_gateway.role_tasker_technical',
    'api_auth_gateway.role_tasker_stem',
    'api_auth_gateway.role_tasker_non_stem',
]

ROLE_LABELS = {'qr': 'QR', 'pl': 'PL', 'tpm': 'TPM'}


class AssignmentController(http.Controller):

    def _role_ids(self, refs):
        return [request.env.ref(r).id for r in refs]

    def _norm_id(self, v):
        if v in (None, False, '', 0, '0'):
            return False
        return int(v)

    @http.route('/api/v2/employees/change_assignment', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'employee_id': {'type': 'int', 'required': True},
    })
    def change_assignment(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}
            user = request.env.user
            actor = user.employee_id

            Employee = request.env['hr.employee'].sudo()
            employee = Employee.browse(int(jdata.get('employee_id')))
            if not employee.exists():
                return return_Response(message='Employee not found', status=404)

            has_qr = 'qr_id' in jdata
            has_pl = 'pl_id' in jdata
            has_tpm = 'tpm_id' in jdata
            if not (has_qr or has_pl or has_tpm):
                return return_Response(
                    message='Provide at least one of qr_id, pl_id, tpm_id',
                    status=400,
                )

            user_role_id = user.user_role.id if user.user_role else 0
            is_cto = user_role_id in self._role_ids(CTO_ROLE_REFS)
            is_tpm = user_role_id in self._role_ids(TPM_ROLE_REFS)
            is_pl = user_role_id in self._role_ids(PL_ROLE_REFS)

            if not (is_cto or is_tpm or is_pl):
                return return_Response(
                    message='Permission denied: only CTO, TPM or PL can change assignments',
                    status=403,
                )

            if is_pl and not (is_cto or is_tpm):
                if has_pl or has_tpm:
                    return return_Response(
                        message='Permission denied: PL can only change QR',
                        status=403,
                    )
                if not actor or employee.task_forge_pl_id.id != actor.id:
                    return return_Response(
                        message='Permission denied: employee is not under your PL scope',
                        status=403,
                    )

            update_vals = {}

            if has_qr:
                new_qr = self._norm_id(jdata.get('qr_id'))
                update_vals['task_forge_qr_id'] = new_qr
                if new_qr:
                    qr_emp = Employee.browse(new_qr)
                    if not qr_emp.exists():
                        return return_Response(message='QR employee not found', status=404)
                    if not has_pl and qr_emp.task_forge_pl_id:
                        update_vals['task_forge_pl_id'] = qr_emp.task_forge_pl_id.id
                        if not has_tpm and qr_emp.task_forge_pl_id.task_forge_tpm_id:
                            update_vals['task_forge_tpm_id'] = qr_emp.task_forge_pl_id.task_forge_tpm_id.id

            if has_pl:
                new_pl = self._norm_id(jdata.get('pl_id'))
                update_vals['task_forge_pl_id'] = new_pl
                if new_pl:
                    pl_emp = Employee.browse(new_pl)
                    if not pl_emp.exists():
                        return return_Response(message='PL employee not found', status=404)
                    if not has_tpm and pl_emp.task_forge_tpm_id:
                        update_vals['task_forge_tpm_id'] = pl_emp.task_forge_tpm_id.id

            if has_tpm:
                new_tpm = self._norm_id(jdata.get('tpm_id'))
                update_vals['task_forge_tpm_id'] = new_tpm
                if new_tpm:
                    tpm_emp = Employee.browse(new_tpm)
                    if not tpm_emp.exists():
                        return return_Response(message='TPM employee not found', status=404)

            old_qr_id = employee.task_forge_qr_id.id
            old_pl_id = employee.task_forge_pl_id.id

            employee.sudo().write(update_vals)

            project_updates = []
            tasker_role_ids = self._role_ids(TASKER_ROLE_REFS)
            employee_role_id = employee.user_id.user_role.id if employee.user_id and employee.user_id.user_role else 0
            if employee_role_id in tasker_role_ids:
                Project = request.env['project.project'].sudo()
                projects = Project.search(
                    Project._task_forge_live_domain() + [('project_tasker', 'in', [employee.id])]
                )
                new_qr_id = employee.task_forge_qr_id.id
                new_pl_id = employee.task_forge_pl_id.id
                for proj in projects:
                    added = []
                    if new_qr_id and new_qr_id != old_qr_id and new_qr_id not in proj.project_qc_reviewer.ids:
                        proj.project_qc_reviewer = [(4, new_qr_id)]
                        added.append('qr')
                    if new_pl_id and new_pl_id != old_pl_id and new_pl_id not in proj.project_lead.ids:
                        proj.project_lead = [(4, new_pl_id)]
                        added.append('pl')
                    if added:
                        project_updates.append({'id': proj.id, 'name': proj.name, 'added': added})

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'Employee Assignment Updated',
                    'message': f'Assignment for "{employee.name}" updated.',
                    'user_id': request.env.user.id,
                    'priority': '1',
                    'res_model': 'hr.employee',
                    'res_id': employee.id,
                })
            except Exception:
                pass

            return return_Response(
                message='Assignment updated successfully',
                status=200,
                data={'data': {
                    'employee_id': employee.id,
                    'qr_id': employee.task_forge_qr_id.id or 0,
                    'qr_name': employee.task_forge_qr_id.name or '',
                    'pl_id': employee.task_forge_pl_id.id or 0,
                    'pl_name': employee.task_forge_pl_id.name or '',
                    'tpm_id': employee.task_forge_tpm_id.id or 0,
                    'tpm_name': employee.task_forge_tpm_id.name or '',
                    'projects_synced': project_updates,
                }},
            )
        except Exception as e:
            _logger.exception('change_assignment failed')
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/employees/assignment_history', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'employee_id': {'type': 'str', 'required': True},
    })
    def get_assignment_history(self, **kwargs):
        try:
            employee_id = int(kwargs.get('employee_id'))
            Employee = request.env['hr.employee'].sudo()
            employee = Employee.browse(employee_id)
            if not employee.exists():
                return return_Response(message='Employee not found', status=404)

            History = request.env['hr.employee.assignment.history'].sudo()
            domain = [('employee_id', '=', employee_id)]

            role_type = kwargs.get('role_type')
            if role_type and role_type in ROLE_LABELS:
                domain.append(('role_type', '=', role_type))

            active = kwargs.get('active')
            if active in ('true', '1', 1, True):
                domain.append(('end_date', '=', False))
            elif active in ('false', '0', 0, False):
                domain.append(('end_date', '!=', False))

            records = History.search(domain, order='start_date desc, id desc')
            data = [{
                'id': rec.id,
                'employee_id': rec.employee_id.id,
                'employee_name': rec.employee_id.name or '',
                'role_type': rec.role_type,
                'role_label': ROLE_LABELS.get(rec.role_type, rec.role_type),
                'from_assignee_id': rec.from_assignee_id.id or 0,
                'from_assignee_name': rec.from_assignee_id.name or '',
                'to_assignee_id': rec.assignee_id.id or 0,
                'to_assignee_name': rec.assignee_id.name or '',
                'start_date': rec.start_date.isoformat() if rec.start_date else '',
                'end_date': rec.end_date.isoformat() if rec.end_date else '',
                'is_active': rec.is_active,
                'duration_days': rec.duration_days,
                'changed_by_id': rec.changed_by_id.id or 0,
                'changed_by_name': rec.changed_by_id.name or '',
            } for rec in records]

            return return_Response(
                message=f'{len(data)} history records found',
                status=200,
                data={'data': data, 'total': len(data)},
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/employees/assignable_employees', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def get_assignable_employees(self, **kwargs):
        try:
            user = request.env.user
            actor = user.employee_id
            user_role_id = user.user_role.id if user.user_role else 0

            cto_ids = self._role_ids(CTO_ROLE_REFS)
            tpm_ids = self._role_ids(TPM_ROLE_REFS)
            pl_ids = self._role_ids(PL_ROLE_REFS)
            qr_ids = self._role_ids(QR_ROLE_REFS)

            is_cto = user_role_id in cto_ids
            is_tpm = user_role_id in tpm_ids
            is_pl = user_role_id in pl_ids

            if not (is_cto or is_tpm or is_pl):
                return return_Response(
                    message='Permission denied: only CTO, TPM or PL can list assignable employees',
                    status=403,
                )

            Employee = request.env['hr.employee'].sudo()

            if is_pl and not (is_cto or is_tpm):
                if not actor:
                    return return_Response(
                        message='No employee record linked to current user',
                        status=400,
                    )
                employees = Employee.search([
                    ('task_forge_pl_id', '=', actor.id),
                    ('user_id.user_role', 'in', qr_ids),
                    ('task_forge_active', '=', True),
                ])
                resolved_role_type = 'qr'
            else:
                role_type = (kwargs.get('role_type') or '').strip().lower()
                if role_type not in ('qr', 'pl'):
                    return return_Response(
                        message='role_type is required and must be one of: QR, PL',
                        status=400,
                    )
                target_role_ids = qr_ids if role_type == 'qr' else pl_ids
                employees = Employee.search([
                    ('user_id.user_role', 'in', target_role_ids),
                    ('task_forge_active', '=', True),
                ])
                resolved_role_type = role_type

            data = [{
                'id': emp.id,
                'name': emp.name or '',
                'role_id': emp.user_id.user_role.id if emp.user_id and emp.user_id.user_role else 0,
                'role_name': emp.user_id.user_role.name if emp.user_id and emp.user_id.user_role else '',
            } for emp in employees]

            return return_Response(
                message=f'{len(data)} employees found',
                status=200,
                data={'data': data, 'role_type': resolved_role_type, 'total': len(data)},
            )
        except Exception as e:
            _logger.exception('get_assignable_employees failed')
            return return_Response(message=str(e), status=400)
