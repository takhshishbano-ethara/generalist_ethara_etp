from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
from datetime import datetime
import json
from odoo import fields


class TaskForgeLeaveController(http.Controller):

    @validate_token
    @http.route('/api/v2/get_leave_types', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_leave_types(self, **kwargs):
        try:
            leave_types = request.env['hr.leave.type'].sudo().search([])
            type_list = []
            for l_type in leave_types:
                type_list.append({
                    'id': l_type.id,
                    'name': l_type.name or ""
                })
            return return_Response(message="Leave types fetched successfully", status=200, data={"record": type_list})
        except Exception as e:
            return return_Response(message="Failed to fetch leave types", status=400, errors=[str(e)])

    @http.route('/api/v2/taskforge/leaves', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'from_date': {'type': 'date', 'required': True},
        'to_date': {'type': 'date', 'required': True},
        'reason': {'type': 'string', 'required': True},
        'holiday_status_id': {'type': 'int', 'required': False},
    })
    def apply_leave(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Leave = request.env['hr.leave'].sudo()
            holiday_status_id = jdata.get('holiday_status_id')
            if not holiday_status_id:
                leave_type = request.env['hr.leave.type'].sudo().search([], limit=1)
                holiday_status_id = leave_type.id if leave_type else False

            if not holiday_status_id:
                return return_Response(message="No leave type configured", status=400)

            leave = Leave.create({
                'employee_id': employee.id,
                'holiday_status_id': holiday_status_id,
                'date_from': jdata['from_date'],
                'date_to': jdata['to_date'],
                'name': jdata['reason'],
            })

            return return_Response(
                message="Leave applied successfully",
                status=200,
                data={'data': self._format_leave(leave)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/leaves', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_leaves(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()
            Leave = request.env['hr.leave'].sudo()
            domain = [('employee_id', 'in', team_ids)]

            status_param = kwargs.get('status')
            if status_param == 'Pending':
                domain.append(('state', '=', 'confirm'))
            elif status_param == 'Approved':
                domain.append(('state', '=', 'validate'))
            elif status_param == 'Rejected':
                domain.append(('state', '=', 'refuse'))

            leaves = Leave.search(domain, order='create_date desc', limit=200)
            data = [self._format_leave(l) for l in leaves]

            return return_Response(message="Leaves list", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/leaves/approve', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'leave_id': {'type': 'int', 'required': True},
    })
    def approve_leave(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_quality_reviewer'):
                return return_Response(message="Insufficient permissions", status=403)

            Leave = request.env['hr.leave'].sudo()
            leave = Leave.browse(jdata['leave_id'])
            if not leave.exists():
                return return_Response(message="Leave not found", status=404)

            leave.action_approve()

            # Notify the employee
            request.env['kubera.notification'].sudo().create({
                'title': 'Leave Approved',
                'message': f'Your leave from {leave.date_from} to {leave.date_to} has been approved.',
                'user_id': leave.employee_id.user_id.id,
                'priority': '1',
                'res_model': 'hr.leave',
                'res_id': leave.id,
            })

            return return_Response(
                message="Leave approved",
                status=200,
                data={'data': self._format_leave(leave)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/leaves/reject', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'leave_id': {'type': 'int', 'required': True},
    })
    def reject_leave(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_quality_reviewer'):
                return return_Response(message="Insufficient permissions", status=403)

            Leave = request.env['hr.leave'].sudo()
            leave = Leave.browse(jdata['leave_id'])
            if not leave.exists():
                return return_Response(message="Leave not found", status=404)

            leave.action_refuse()

            request.env['kubera.notification'].sudo().create({
                'title': 'Leave Rejected',
                'message': f'Your leave from {leave.date_from} to {leave.date_to} has been rejected.',
                'user_id': leave.employee_id.user_id.id,
                'priority': '1',
                'res_model': 'hr.leave',
                'res_id': leave.id,
            })

            return return_Response(
                message="Leave rejected",
                status=200,
                data={'data': self._format_leave(leave)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/leaves/hierarchy', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def leave_hierarchy(self, **kwargs):
        try:
            user = request.env.user
            if user.user_role.id != self.env.ref('api_auth_gateway.role_cto_technical').id:
                return return_Response(message="Admin access required", status=403)

            Employee = request.env['hr.employee'].sudo()
            Leave = request.env['hr.leave'].sudo()
            today = datetime.now().date()
            pls = Employee.search([('user_id.user_role', 'in', [request.env.ref('api_auth_gateway.role_pl_non_stem').id, request.env.ref('api_auth_gateway.role_pl_technical').id, request.env.ref('api_auth_gateway.role_pl_stem').id])])
            hierarchy = []

            for pl in pls:
                pl_leaves = Leave.search_count([
                    ('employee_id', '=', pl.id),
                    ('date_from', '<=', today),
                    ('date_to', '>=', today),
                    ('state', '=', 'validate'),
                ])
                qrs = Employee.search([('task_forge_pl_id', '=', pl.id), ('task_forge_active', '=', True)])
                qr_data = []
                for qr in qrs:
                    taskers = Employee.search([('task_forge_qr_id', '=', qr.id), ('task_forge_active', '=', True)])
                    tasker_data = []
                    for t in taskers:
                        on_leave = Leave.search_count([
                            ('employee_id', '=', t.id),
                            ('date_from', '<=', today),
                            ('date_to', '>=', today),
                            ('state', '=', 'validate'),
                        ])
                        pending = Leave.search_count([
                            ('employee_id', '=', t.id),
                            ('state', '=', 'confirm'),
                        ])
                        tasker_data.append({
                            'id': t.id,
                            'name': t.name,
                            'on_leave': bool(on_leave),
                            'pending_leaves': pending,
                        })
                    qr_data.append({
                        'id': qr.id,
                        'name': qr.name,
                        'taskers': tasker_data,
                    })
                hierarchy.append({
                    'id': pl.id,
                    'name': pl.name,
                    'on_leave': bool(pl_leaves),
                    'qrs': qr_data,
                })

            return return_Response(message="Leave hierarchy", status=200, data={'data': hierarchy})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _format_leave(self, leave):
        state_map = {
            'draft': 'Draft',
            'confirm': 'Pending',
            'validate1': 'Pending',
            'validate': 'Approved',
            'refuse': 'Rejected',
        }
        return {
            'id': leave.id if leave.id else 0,
            'employee_id': leave.employee_id.id if leave.employee_id.id else 0,
            'employee_name': leave.employee_id.name if leave.employee_id.name else "",
            'role': leave.employee_id.user_id.user_role.name if leave.employee_id.user_id.user_role.name else "",
            'from_date': str(leave.date_from) if leave.date_from else '',
            'to_date': str(leave.date_to) if leave.date_to else '',
            'reason': leave.name or '',
            'status': state_map.get(leave.state, leave.state),
            'is_paid': leave.is_paid,
            'approved_by_name': leave.first_approver_id.name if leave.first_approver_id else '',
            'created_at': leave.create_date.isoformat() if leave.create_date else '',
        }

    @http.route('/api/v2/taskforge/today_leaves_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def today_leaves_list(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()
            Leave = request.env['hr.leave'].sudo()
            # current_projects = request.env['project.project'].sudo().search([])
            # if kwargs.get('project_id'):
            #     current_projects = request.env['project.project'].sudo().search([('id', '=', kwargs['project_id'])],
            #                                                                     limit=1)
            # employee_list = current_projects.mapped('project_lead') | current_projects.mapped('project_tasker') | current_projects.mapped('project_qc_reviewer')

            domain = [
                ('employee_id', 'in', team_ids),
                ('state', '=', 'validate'),
                ('date_from', '<=',
                 fields.Datetime.to_string(fields.Datetime.now().replace(hour=23, minute=59, second=59))),
                ('date_to', '>=', fields.Datetime.to_string(fields.Datetime.now().replace(hour=0, minute=0, second=0)))
            ]

            leaves = Leave.search(domain, order='create_date desc', limit=200)
            data = [self._format_leave(l) for l in leaves]

            return return_Response(message="Leaves list", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

