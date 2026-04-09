from odoo import http, fields
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
import json
from datetime import datetime, date, timedelta

class EmployeeController(http.Controller):

    @http.route('/api/v2/employees', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'name': {'type': 'string', 'required': True},
        'email': {'type': 'email', 'required': True},
        'job_title': {'type': 'int', 'required': True},
        'role_id': {'type': 'int', 'required': True},
    })
    def create_employee(self, **kwargs):
        try:
            jdata = kwargs.get('jdata', {})
            user = request.env.user
            Employee = request.env['hr.employee'].sudo()
            ResUsers = request.env['res.users'].sudo()

            email = jdata.get('email', '').strip().lower()
            if not email.endswith('@ethara.ai'):
                return return_Response(message="Email must be @ethara.ai domain", status=400)

            existing_user = ResUsers.search([('login', '=', email)], limit=1)
            if existing_user:
                return return_Response(message=f"User with email {email} already exists", status=400)

            user_vals = {
                'name': jdata['name'],
                'login': email,
                'email': email,
                'user_role': jdata.get('role_id'),
                'password': 'Ethara@123',
            }
            new_user = ResUsers.create(user_vals)

            employee_vals = {
                'name': jdata['name'],
                'work_email': email,
                'user_id': new_user.id,
                'designation_id': jdata.get('job_title'),
                'work_location_name': jdata.get('work_location_name', ''),
            }

            if jdata.get('department_id'):
                employee_vals['department_id'] = jdata['department_id']
            if jdata.get('project_id'):
                employee_vals['project_id'] = jdata['project_id']
            if jdata.get('pl_id'):
                employee_vals['task_forge_pl_id'] = jdata['pl_id']
            if jdata.get('qr_id'):
                employee_vals['task_forge_qr_id'] = jdata['qr_id']

            employee = Employee.create(employee_vals)

            return return_Response(
                message="Employee created successfully",
                status=200,
                data={'data': {
                    'id': employee.id,
                    'name': employee.name,
                    'email': employee.work_email,
                    'job_title': employee.job_title,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/employees/bulk', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def bulk_create_employees(self, **kwargs):
        """Bulk create employees from JSON array"""
        try:
            data = {}
            try:
                data = json.loads(request.httprequest.stream.read())
            except:
                data = json.loads(request.httprequest.data)

            employees_data = data.get('employees', [])
            if not employees_data:
                return return_Response(message="No employees data provided", status=400)

            Employee = request.env['hr.employee'].sudo()
            ResUsers = request.env['res.users'].sudo()

            created = []
            errors = []

            for idx, emp_data in enumerate(employees_data, start=1):
                try:
                    name = emp_data.get('name', '').strip()
                    email = emp_data.get('email', '').strip().lower()

                    if not name:
                        errors.append(f"Row {idx}: name is required")
                        continue
                    if not email:
                        errors.append(f"Row {idx}: email is required")
                        continue
                    if not email.endswith('@ethara.ai'):
                        errors.append(f"Row {idx}: email must be @ethara.ai")
                        continue

                    existing_user = ResUsers.search([('login', '=', email)], limit=1)
                    if existing_user:
                        errors.append(f"Row {idx}: {email} already exists")
                        continue

                    user_vals = {
                        'name': name,
                        'login': email,
                        'email': email,
                        'password': emp_data.get('password', 'Ethara@123'),
                    }
                    new_user = ResUsers.create(user_vals)

                    employee_vals = {
                        'name': name,
                        'work_email': email,
                        'user_id': new_user.id,
                        'job_title': emp_data.get('job_title', ''),
                        'work_location_name': emp_data.get('work_location_name', ''),
                    }

                    if emp_data.get('department_id'):
                        employee_vals['department_id'] = emp_data['department_id']
                    if emp_data.get('pl_id'):
                        employee_vals['task_forge_pl_id'] = emp_data['pl_id']
                    if emp_data.get('qr_id'):
                        employee_vals['task_forge_qr_id'] = emp_data['qr_id']

                    employee = Employee.create(employee_vals)
                    created.append({
                        'id': employee.id,
                        'name': employee.name,
                        'email': employee.work_email,
                    })
                except Exception as e:
                    errors.append(f"Row {idx}: {str(e)}")

            return return_Response(
                message=f"Bulk create complete: {len(created)} created, {len(errors)} errors",
                status=200,
                data={'data': {'created': created, 'errors': errors}}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v1/employees/<int:employee_id>', methods=['PUT', 'PATCH'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'name': {'type': 'string', 'required': False},
        'job_title': {'type': 'string', 'required': False},
        'department_id': {'type': 'int', 'required': False},
        'project_id': {'type': 'int', 'required': False},
        'work_location_name': {'type': 'string', 'required': False},
        'pl_id': {'type': 'int', 'required': False},
        'qr_id': {'type': 'int', 'required': False},
    })
    def update_employee(self, employee_id, jdata=None, **kwargs):
        """Update employee details"""
        try:
            Employee = request.env['hr.employee'].sudo()
            employee = Employee.browse(employee_id)

            if not employee.exists():
                return return_Response(message="Employee not found", status=404)

            allowed_fields = [
                'name', 'job_title', 'department_id', 'project_id',
                'work_location_name', 'task_forge_pl_id', 'task_forge_qr_id'
            ]

            update_vals = {}
            for field in allowed_fields:
                if jdata.get(field):
                    if field == 'pl_id':
                        update_vals['task_forge_pl_id'] = jdata.get(field)
                    elif field == 'qr_id':
                        update_vals['task_forge_qr_id'] = jdata.get(field)
                    else:
                        update_vals[field] = jdata.get(field)

            if update_vals:
                employee.write(update_vals)

            return return_Response(
                message="Employee updated successfully",
                status=200,
                data={'data': {
                    'id': employee.id,
                    'name': employee.name,
                    'job_title': employee.job_title,
                    'work_location': employee.work_location_name,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v1/employees/offboard', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'employee_id': {'type': 'int', 'required': True},
        'action': {'type': 'string', 'required': True},
    })
    def offboard_employee(self, **kwargs):
        """Offboard employee - action: start, complete, reactivate"""
        try:
            Employee = request.env['hr.employee'].sudo()
            employee = Employee.browse(int(kwargs.get('employee_id')))

            if not employee.exists():
                return return_Response(message="Employee not found", status=404)

            action = kwargs.get('action', '').lower()

            if action == 'start':
                if employee.offboarding_state != 'active':
                    return return_Response(
                        message=f"Cannot start offboarding. Current state: {employee.offboarding_state}",
                        status=400
                    )
                employee.write({
                    'offboarding_state': 'offboarding',
                    'offboard_date': fields.Date.today(),
                })
                return return_Response(
                    message="Offboarding started",
                    status=200,
                    data={'data': {
                        'id': employee.id,
                        'offboarding_state': employee.offboarding_state,
                        'offboard_date': employee.offboard_date,
                    }}
                )

            elif action == 'complete':
                if employee.offboarding_state != 'offboarding':
                    return return_Response(
                        message=f"Cannot complete offboarding. Current state: {employee.offboarding_state}",
                        status=400
                    )
                employee.write({
                    'offboarding_state': 'offboarded',
                    'active': False,
                })
                return return_Response(
                    message="Employee offboarded successfully",
                    status=200,
                    data={'data': {
                        'id': employee.id,
                        'offboarding_state': employee.offboarding_state,
                    }}
                )

            elif action == 'reactivate':
                if employee.offboarding_state == 'active':
                    return return_Response(message="Employee is already active", status=400)

                employee.write({
                    'offboarding_state': 'active',
                    'active': True,
                    'offboard_date': False,
                })
                return return_Response(
                    message="Employee reactivated successfully",
                    status=200,
                    data={'data': {
                        'id': employee.id,
                        'offboarding_state': employee.offboarding_state,
                    }}
                )

            else:
                return return_Response(
                    message="Invalid action. Use: start, complete, or reactivate",
                    status=400
                )

        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v1/employees', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'employee_id': {'type': 'int', 'required': True},
    })
    def get_employee(self, **kwargs):
        """Get employee details"""
        try:
            Employee = request.env['hr.employee'].sudo()
            employee = Employee.browse(int(kwargs['employee_id']))

            if not employee.exists():
                return return_Response(message="Employee not found", status=404)

            return return_Response(
                message="Employee details",
                status=200,
                data={'data': {
                    'id': employee.id,
                    'name': employee.name,
                    'email': employee.work_email,
                    'designation_id': employee.designation_id.id,
                    'role': employee.user_id.user_role.name,
                    'job_title': employee.designation_id.name,
                    'department': employee.department_id.name if employee.department_id else '',
                    'work_location': employee.work_location_name,
                    'offboarding_state': employee.offboarding_state,
                    'is_offboarded': employee.is_offboarded,
                    'offboard_date': employee.offboard_date.isoformat() if employee.offboard_date else None,
                    'pl_id': employee.task_forge_pl_id.id if employee.task_forge_pl_id else None,
                    'pl_name': employee.task_forge_pl_id.name if employee.task_forge_pl_id else None,
                    'qr_id': employee.task_forge_qr_id.id if employee.task_forge_qr_id else None,
                    'qr_name': employee.task_forge_qr_id.name if employee.task_forge_qr_id else None,
                    'active': employee.active,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v1/employees_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_employees(self, **kwargs):
        """List employees with filters"""
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)
            team_ids = employee._get_team_employee_ids()

            Employee = request.env['hr.employee'].sudo()

            domain = [('employee_id', 'in', team_ids)]
            if kwargs.get('active') == 'true':
                domain.append(('active', '=', True))
            elif kwargs.get('active') == 'false':
                domain.append(('active', '=', False))

            if kwargs.get('offboarding_state'):
                domain.append(('offboarding_state', '=', kwargs['offboarding_state']))

            if kwargs.get('role'):
                domain.append(('user_id.user_role', '=', int(kwargs['role'])))

            if kwargs.get('department_id'):
                domain.append(('department_id', '=', int(kwargs['department_id'])))

            limit = int(kwargs.get('limit', 100))
            employees = Employee.search(domain, limit=limit)
            data = []
            for emp in employees:
                today_task = request.env['task.forge.log'].sudo().search([('employee_id', '=', emp.id), ('state', '=', 'in_progress')], order='write_date desc')
                data.append({
                    'id': emp.id,
                    'name': emp.name,
                    'email': emp.work_email,
                    'job_title': emp.job_title,
                    'department': emp.department_id.name if emp.department_id else '',
                    'offboarding_state': emp.offboarding_state,
                    'is_offboarded': emp.is_offboarded,
                    'designation_id': emp.designation_id.id,
                    'role': emp.user_id.user_role.name,
                    'current_status': self.get_employee_current_status(emp),
                    'current_task': today_task[0].name if today_task else "",
                    'task_today': len(today_task),
                    'work_location': emp.work_location_name,
                    'offboard_date': emp.offboard_date.isoformat() if emp.offboard_date else None,
                    'pl_id': emp.task_forge_pl_id.id if emp.task_forge_pl_id else None,
                    'pl_name': emp.task_forge_pl_id.name if emp.task_forge_pl_id else None,
                    'qr_id': emp.task_forge_qr_id.id if emp.task_forge_qr_id else None,
                    'qr_name': emp.task_forge_qr_id.name if emp.task_forge_qr_id else None,
                    'active': emp.active,
                    'last_active': str(today_task.write_date) if today_task else ""
                })
            return return_Response(
                message=f"{len(data)} employees found",
                status=200,
                data={'data': data}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/employees/<int:employee_id>', methods=['DELETE'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def delete_employee(self, employee_id, **kwargs):
        """Soft delete (archive) an employee"""
        try:
            Employee = request.env['hr.employee'].sudo()
            employee = Employee.browse(employee_id)

            if not employee.exists():
                return return_Response(message="Employee not found", status=404)

            employee.write({'active': False})

            return return_Response(
                message="Employee archived successfully",
                status=200
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/get_user_role', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_user_role(self, **kwargs):
        temp = []
        try:
            domain = [('project_type', '=', 'non-stem')]
            if kwargs.get('project_type'):
                domain = [('project_type', '=', kwargs.get('project_type'))]
            roles = request.env['api.role'].sudo().search(domain)
            for role in roles:
                temp.append({'id': role.id, 'name': role.name})
            return return_Response(
                message="success",
                status=200,
                data={'data': temp}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    def get_employee_current_status(self, emp):
        TaskLog = request.env['task.forge.log'].sudo()
        tasks = TaskLog.search_count([
            ('employee_id', '=', emp.id),
            ('state', '=', 'in_progress'),
        ])
        today = date.today()
        Attendance = request.env['hr.attendance'].sudo()
        attendance = Attendance.search([
            ('employee_id', '=', emp.id),
            ('check_in', '>=', datetime.combine(today, datetime.min.time())),
            ('check_in', '<', datetime.combine(today, datetime.max.time())),
            ('attendance_status', '=', 'present')
        ], limit=1)
        current_status = ""
        if not attendance:
            current_status = "Offline"
        elif tasks:
            current_status = "Active"
        else:
            current_status = "Idle"
        return current_status

