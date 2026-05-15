from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request,
)
import logging

_logger = logging.getLogger(__name__)

ROLE_TYPE_TO_PROJECT_FIELD = {
    'pl': 'project_lead',
    'qr': 'project_qc_reviewer',
    'tasker': 'project_tasker',
    'swe': 'project_swe',
    'aire': 'project_aire',
}


class TaskForgeRoleManagementController(http.Controller):

    @http.route('/api/v2/taskforge/role_management/counts', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def role_counts(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            ref = request.env.ref
            ResUsers = request.env['res.users'].sudo()

            cto_ids = [ref('api_auth_gateway.role_cto_technical').id]
            pl_ids = [
                ref('api_auth_gateway.role_pl_technical').id,
                ref('api_auth_gateway.role_pl_stem').id,
                ref('api_auth_gateway.role_pl_non_stem').id,
            ]
            qr_ids = [
                ref('api_auth_gateway.role_qc_technical').id,
                ref('api_auth_gateway.role_qc_stem').id,
                ref('api_auth_gateway.role_qc_non_stem').id,
            ]
            tasker_ids = [
                ref('api_auth_gateway.role_tasker_technical').id,
                ref('api_auth_gateway.role_tasker_stem').id,
                ref('api_auth_gateway.role_tasker_non_stem').id,
            ]

            total_employee = request.env['hr.employee'].sudo().search_count([('task_forge_active', '=', True)])
            pl_count = request.env['hr.employee'].sudo().search_count([('user_id.user_role', 'in', pl_ids), ('task_forge_active', '=', True)])
            qr_count = request.env['hr.employee'].sudo().search_count([('user_id.user_role', 'in', qr_ids), ('task_forge_active', '=', True)])
            tasker_count = request.env['hr.employee'].sudo().search_count([('user_id.user_role', 'in', tasker_ids), ('task_forge_active', '=', True)])
            cto_count = request.env['hr.employee'].sudo().search_count([('user_id.user_role', 'in', cto_ids), ('task_forge_active', '=', True)])

            return return_Response(
                message="Success",
                status=200,
                data={
                    'total_employee': total_employee,
                    'cto_count': cto_count,
                    'pl_count': pl_count,
                    'qr_count': qr_count,
                    'tasker_count': tasker_count,
                }
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/role_management/employees', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def employee_list(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Employee = request.env['hr.employee'].sudo()
            domain = [('task_forge_active', '=', True)]

            if kwargs.get('role'):
                domain.append(('user_id.user_role', '=', int(kwargs.get('role'))))

            if kwargs.get('job_title'):
                domain.append(('designation_id', '=', int(kwargs.get('job_title'))))

            if kwargs.get('search'):
                domain += [
                    '|',
                    ('name', 'ilike', kwargs['search']),
                    ('work_email', 'ilike', kwargs['search']),
                ]

            page = int(kwargs.get('page')) if kwargs.get('page') else 1
            limit = int(kwargs.get('limit')) if kwargs.get('limit') else 10
            offset = (page - 1) * limit
            total_count = Employee.search_count(domain)
            if not kwargs.get('page'):
                limit = total_count or 1
                offset = 0
            employees = Employee.search(domain, order='name asc', limit=limit, offset=offset)

            data = []
            TaskLog = request.env['task.forge.log'].sudo()
            for emp in employees:
                role_name = ''
                if emp.user_id and emp.user_id.user_role:
                    role_name = emp.user_id.user_role.name or ''
                reviewed_count = TaskLog.search_count([
                    ('employee_id', '=', emp.id),
                    ('state', 'in', ['qc_approved', 'qc_rejected']),
                ])
                data.append({
                    'id': emp.id or 0,
                    'name': emp.name or '',
                    'email': emp.work_email or '',
                    'role': role_name or '',
                    'date_of_joining': emp.create_date.strftime('%Y-%m-%d') if emp.create_date else '',
                    'designation_id': emp.designation_id.id if emp.designation_id.id else 0,
                    'job_title': emp.designation_id.name if emp.designation_id and emp.designation_id.name else '',
                    'task_reviewed_count': reviewed_count,
                })

            return return_Response(
                message="Success",
                status=200,
                data={
                    'record': data,
                    'total_record_count': total_count,
                    'count': len(data),
                }
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _get_role_type(self, role_id):
        """Map an api.role record ID to a role type string."""
        ref = request.env.ref
        role_map = {
            ref('api_auth_gateway.role_cto_technical').id: 'cto',
            ref('api_auth_gateway.role_pl_technical').id: 'pl',
            ref('api_auth_gateway.role_pl_stem').id: 'pl',
            ref('api_auth_gateway.role_pl_non_stem').id: 'pl',
            ref('api_auth_gateway.role_qc_technical').id: 'qr',
            ref('api_auth_gateway.role_qc_stem').id: 'qr',
            ref('api_auth_gateway.role_qc_non_stem').id: 'qr',
            ref('api_auth_gateway.role_tasker_technical').id: 'tasker',
            ref('api_auth_gateway.role_tasker_stem').id: 'tasker',
            ref('api_auth_gateway.role_tasker_non_stem').id: 'tasker'
        }
        return role_map.get(role_id, '')

    @http.route('/api/v2/taskforge/role_management/change_role', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'employee_id': {'type': 'int', 'required': True},
        'new_role_id': {'type': 'int', 'required': True},
    })
    def change_employee_role(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            employee_id = int(jdata.get('employee_id'))
            new_role_id = int(jdata.get('new_role_id'))

            current_user = request.env.user
            current_emp = current_user.employee_id
            if not current_emp:
                return return_Response(message="Employee profile not found", status=404)

            current_role = current_emp._get_task_forge_role()
            if current_role != 'admin':
                return return_Response(message="Only CTO can change employee roles", status=403)

            Employee = request.env['hr.employee'].sudo()
            target_emp = Employee.browse(employee_id)
            if not target_emp.exists():
                return return_Response(message="Target employee not found", status=404)

            target_user = target_emp.user_id
            if not target_user:
                return return_Response(message="Employee has no linked user account", status=400)

            new_role = request.env['api.role'].sudo().browse(new_role_id)
            if not new_role.exists():
                return return_Response(message="Role not found", status=404)

            old_role_id = target_user.user_role.id if target_user.user_role else None
            old_role_name = target_user.user_role.name if target_user.user_role else ''
            old_role_type = self._get_role_type(old_role_id) if old_role_id else ''
            new_role_type = self._get_role_type(new_role_id)

            if old_role_id == new_role_id:
                return return_Response(message="Employee already has this role", status=400)

            target_user.sudo().write({'user_role': new_role_id})
            if jdata.get('pl_id') or jdata.get('qr_id'):
                emp_vals = {}
                if jdata.get('pl_id'):
                    emp_vals['task_forge_pl_id'] = int(jdata['pl_id'])
                if jdata.get('qr_id'):
                    emp_vals['task_forge_qr_id'] = int(jdata['qr_id'])
                if emp_vals:
                    target_emp.sudo().write(emp_vals)
            Project = request.env['project.project'].sudo()
            old_field = ROLE_TYPE_TO_PROJECT_FIELD.get(old_role_type)
            new_field = ROLE_TYPE_TO_PROJECT_FIELD.get(new_role_type)

            projects_updated = []
            if old_field:
                affected_projects = Project.search([
                    (old_field, 'in', [target_emp.id]),
                ] + Project._task_forge_live_domain())
                for proj in affected_projects:
                    write_vals = {old_field: [(3, target_emp.id)]}
                    if new_field:
                        write_vals[new_field] = [(4, target_emp.id)]
                    proj.write(write_vals)
                    projects_updated.append({
                        'project_id': proj.id,
                        'project_name': proj.name or '',
                        'removed_from': old_field,
                        'added_to': new_field or '',
                    })

            return return_Response(
                message="Role changed successfully",
                status=200,
                data={
                    'employee_id': target_emp.id,
                    'employee_name': target_emp.name or '',
                    'old_role': old_role_name,
                    'new_role': new_role.name or '',
                    'projects_updated': len(projects_updated),
                    'project_details': projects_updated,
                }
            )
        except Exception as e:
            _logger.error('Change role failed: %s', str(e))
            return return_Response(message=str(e), status=400)
