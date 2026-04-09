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
        'employee_id': {'type': 'str', 'required': True},
    })
    def get_employee(self, **kwargs):
        """Get employee details"""
        try:
            Employee = request.env['hr.employee'].sudo()
            employee = Employee.browse(int(kwargs['employee_id']))

            if not employee.exists():
                return return_Response(message="Employee not found", status=404)
            today = date.today()
            start_this_week = today - timedelta(days=today.weekday())
            start_last_week = start_this_week - timedelta(days=7)
            end_last_week = start_this_week - timedelta(days=1)
            Log = request.env['task.forge.log'].sudo()

            def get_prod_stats(target_emp_id, start_date, end_date):
                domain = [
                    ('employee_id', '=', target_emp_id),
                    ('date', '>=', start_date),
                    ('date', '<=', end_date)
                ]
                total = Log.search_count(domain)
                done = Log.search_count(domain + [('state', '=', 'completed')])

                percentage = (done / total * 100) if total > 0 else 0.0
                return round(percentage, 2), total

            last_7_days = [str(today - timedelta(days=i)) for i in range(6, -1, -1)]

            productivity_report = {
                "labels": last_7_days,
                'values': []
            }

            for day in last_7_days:
                domain = [
                    ('employee_id', '=', employee.id),
                    ('date', '=', day)
                ]

                total = Log.search_count(domain)
                done = Log.search_count(domain + [('state', '=', 'completed')])

                # Calculate percentage
                percentage = (done / total * 100) if total > 0 else 0.0

                productivity_report['values'].append(round(percentage, 2))
            # Calculations
            this_week_prod, _ = get_prod_stats(employee.id, start_this_week, today)
            last_week_prod, _ = get_prod_stats(employee.id, start_last_week, end_last_week)
            task_record = request.env['task.forge.log'].sudo().search([('employee_id', '=', employee.id), ('date', '=', today)])
            active_task = request.env['task.forge.log'].sudo().search([('employee_id', '=', employee.id), ('date', '=', today), ('state', '=', 'in_progress')], order='write_date desc', limit=1)
            punch_in_status, emp_session = self.get_employee_current_status(employee)
            return return_Response(
                message="Employee details",
                status=200,
                data={'data': {
                    'id': employee.id if employee else 0,
                    'name': employee.name if employee and employee.name else "",
                    'email': employee.work_email if employee and employee.work_email else "",
                    'mobile': employee.work_phone if employee and employee.work_phone else "",
                    'job_title_id': employee.designation_id.id if employee.designation_id else 0,
                    'job_title': employee.designation_id.name if employee.designation_id and employee.designation_id.name else "",
                    'department_id': employee.department_id.id if employee.department_id else 0,
                    'department': employee.department_id.name if employee.department_id and employee.department_id.name else '',
                    'offboarding_state': employee.offboarding_state or "",
                    'is_offboarded': employee.is_offboarded,
                    'role_id': employee.user_id.user_role.id if employee.user_id.user_role else 0,
                    'role': employee.user_id.user_role.name if employee.user_id.user_role and employee.user_id.user_role.name else "",
                    'current_status': punch_in_status,
                    'emp_session': emp_session,
                    'this_week_productivity': this_week_prod,
                    'last_week_productivity': last_week_prod,
                    'current_task': active_task.name if active_task else "",
                    'task_today': len(task_record),
                    'today_task_record': [{'id': t.id, 'name': t.name, 'status': t.state or ""} for t in task_record],
                    'work_location': employee.work_location_name or "",
                    'offboard_date': employee.offboard_date.isoformat() if employee.offboard_date else "",
                    'pl_id': employee.task_forge_pl_id.id if employee.task_forge_pl_id else 0,
                    'pl_name': employee.task_forge_pl_id.name if employee.task_forge_pl_id and employee.task_forge_pl_id.name else "",
                    'qr_id': employee.task_forge_qr_id.id if employee.task_forge_qr_id else 0,
                    'qr_name': employee.task_forge_qr_id.name if employee.task_forge_qr_id and employee.task_forge_qr_id.name else "",
                    'active': employee.active or False,
                    'productivity_report': productivity_report,
                    'last_active': str(active_task.write_date) if active_task else ""
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
            search = kwargs.get('search')
            if search:
                domain += [('name', 'ilike', search)]

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
            today = date.today()
            start_this_week = today - timedelta(days=today.weekday())
            for emp in employees:
                Log = request.env['task.forge.log'].sudo()
                def get_prod_stats(target_emp_id, start_date, end_date):
                    domain = [
                        ('employee_id', '=', target_emp_id),
                        ('date', '>=', start_date),
                        ('date', '<=', end_date)
                    ]
                    total = Log.search_count(domain)
                    done = Log.search_count(domain + [('state', '=', 'completed')])

                    percentage = (done / total * 100) if total > 0 else 0.0
                    return round(percentage, 2), total

                this_week_prod, _ = get_prod_stats(emp.id, start_this_week, today)
                task_record = request.env['task.forge.log'].sudo().search_count([('employee_id', '=', employee.id), ('date', '=', today)])
                active_task = request.env['task.forge.log'].sudo().search([('employee_id', '=', employee.id), ('date', '=', today), ('state', '=', 'in_progress')], order='write_date desc', limit=1)
                current_status, _ =  self.get_employee_current_status(emp)
                data.append({
                    'id': emp.id if emp else 0,
                    'name': emp.name if emp and emp.name else "",
                    'email': emp.work_email if emp and emp.work_email else "",
                    'job_title_id': emp.designation_id.id if emp.designation_id else 0,
                    'job_title': emp.designation_id.name if emp.designation_id and emp.designation_id.name else "",
                    'department_id': emp.department_id.id if emp.department_id else 0,
                    'department': emp.department_id.name if emp.department_id and emp.department_id.name else '',
                    'offboarding_state': emp.offboarding_state or "",
                    'is_offboarded': emp.is_offboarded,
                    'role_id': emp.user_id.user_role.id if emp.user_id.user_role else 0,
                    'role': emp.user_id.user_role.name if emp.user_id.user_role and emp.user_id.user_role.name else "",
                    'current_status': current_status,
                    'current_task': active_task.name if active_task else "",
                    'productivity': this_week_prod,
                    'task_today': task_record,
                    'work_location': emp.work_location_name or "",
                    'offboard_date': emp.offboard_date.isoformat() if emp.offboard_date else "",
                    'pl_id': emp.task_forge_pl_id.id if emp.task_forge_pl_id else 0,
                    'pl_name': emp.task_forge_pl_id.name if emp.task_forge_pl_id and emp.task_forge_pl_id.name else "",
                    'qr_id': emp.task_forge_qr_id.id if emp.task_forge_qr_id else 0,
                    'qr_name': emp.task_forge_qr_id.name if emp.task_forge_qr_id and emp.task_forge_qr_id.name else "",
                    'active': emp.active or False,
                    'last_active': str(active_task.write_date) if active_task else ""
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

        duration_display = "00:00"
        if attendance and attendance.check_in and attendance.check_out:
            diff = attendance.check_out - attendance.check_in
            total_seconds = int(diff.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            duration_display = f"{hours:02d}:{minutes:02d}"

        elif attendance and attendance.check_in and not attendance.check_out:
            diff = datetime.now() - attendance.check_in
            total_seconds = int(diff.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            duration_display = f"{hours:02d}:{minutes:02d}"

        current_status = ""
        if not attendance:
            current_status = "Offline"
        elif tasks:
            current_status = "Active"
        else:
            current_status = "Idle"
        return current_status, duration_display

