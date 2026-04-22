from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token,
)


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
            for emp in employees:
                role_name = ''
                if emp.user_id and emp.user_id.user_role:
                    role_name = emp.user_id.user_role.name or ''
                data.append({
                    'id': emp.id or 0,
                    'name': emp.name or '',
                    'email': emp.work_email or '',
                    'role': role_name or '',
                    'date_of_joining': emp.create_date.strftime('%Y-%m-%d') if emp.create_date else '',
                    'designation_id': emp.designation_id.id if emp.designation_id.id else 0,
                    'job_title': emp.designation_id.name if emp.designation_id and emp.designation_id.name else ''
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
