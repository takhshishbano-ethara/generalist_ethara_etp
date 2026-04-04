from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
from datetime import datetime, date
import json


class TaskForgeAttendanceController(http.Controller):

    @http.route('/api/v2/taskforge/attendance/punch_in', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'location': {'type': 'string', 'required': False},
        'geo_coordinates': {'type': 'string', 'required': False},
    })
    def punch_in(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            today = date.today()
            Attendance = request.env['hr.attendance'].sudo()
            existing = Attendance.search([
                ('employee_id', '=', employee.id),
                ('check_in', '>=', datetime.combine(today, datetime.min.time())),
                ('check_in', '<', datetime.combine(today, datetime.max.time())),
            ], limit=1)

            if existing:
                return return_Response(message="Already punched in today", status=400)

            vals = {
                'employee_id': employee.id,
                'check_in': datetime.now(),
            }
            if jdata.get('location'):
                vals['geo_location'] = jdata['location']
            if jdata.get('geo_coordinates'):
                vals['geo_coordinates'] = jdata['geo_coordinates']

            attendance = Attendance.create(vals)

            return return_Response(
                message="Punched in successfully",
                status=200,
                data={'data': {
                    'id': attendance.id,
                    'employee_id': employee.id,
                    'employee_name': employee.name,
                    'date': str(today),
                    'punch_in_time': attendance.check_in.isoformat() if attendance.check_in else None,
                    'location': attendance.geo_location or '',
                    'geo_coordinates': attendance.geo_coordinates or '',
                    'status': 'Present',
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/attendance/punch_out', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def punch_out(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            today = date.today()
            Attendance = request.env['hr.attendance'].sudo()
            attendance = Attendance.search([
                ('employee_id', '=', employee.id),
                ('check_in', '>=', datetime.combine(today, datetime.min.time())),
                ('check_in', '<', datetime.combine(today, datetime.max.time())),
                ('check_out', '=', False),
            ], limit=1)

            if not attendance:
                return return_Response(message="No active punch-in found for today", status=400)

            attendance.write({'check_out': datetime.now()})

            return return_Response(
                message="Punched out successfully",
                status=200,
                data={'data': {
                    'id': attendance.id,
                    'employee_id': employee.id,
                    'employee_name': employee.name,
                    'date': str(today),
                    'punch_in_time': attendance.check_in.isoformat() if attendance.check_in else None,
                    'punch_out_time': attendance.check_out.isoformat() if attendance.check_out else None,
                    'hours_worked': round(attendance.worked_hours, 2) if attendance.worked_hours else 0,
                    'status': 'Present',
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/attendance/today', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def attendance_today(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            today = date.today()
            Attendance = request.env['hr.attendance'].sudo()
            attendance = Attendance.search([
                ('employee_id', '=', employee.id),
                ('check_in', '>=', datetime.combine(today, datetime.min.time())),
                ('check_in', '<', datetime.combine(today, datetime.max.time())),
            ], limit=1)

            if not attendance:
                return return_Response(message="No attendance record for today", status=200, data={'data': None})

            return return_Response(
                message="Today's attendance",
                status=200,
                data={'data': self._format_attendance(attendance)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/attendance', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_attendance(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()
            Attendance = request.env['hr.attendance'].sudo()

            domain = [('employee_id', 'in', team_ids)]
            user_id_param = kwargs.get('user_id')
            if user_id_param:
                domain.append(('employee_id', '=', int(user_id_param)))

            records = Attendance.search(domain, order='check_in desc', limit=200)

            data = [self._format_attendance(rec) for rec in records]

            return return_Response(message="Attendance list", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _format_attendance(self, rec):
        return {
            'id': rec.id,
            'employee_id': rec.employee_id.id,
            'employee_name': rec.employee_id.name,
            'role': rec.employee_id.user_id.user_role.name,
            'date': str(rec.check_in.date()) if rec.check_in else '',
            'status': 'Present',
            'punch_in_time': rec.check_in.isoformat() if rec.check_in else None,
            'punch_out_time': rec.check_out.isoformat() if rec.check_out else None,
            'hours_worked': round(rec.worked_hours, 2) if rec.worked_hours else 0,
            'location': rec.geo_location or '',
            'geo_coordinates': rec.geo_coordinates or '',
            'tasks_done': rec.tasks_done,
        }


    @http.route('/api/v2/taskforge/all_employee_attendance/today', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def all_employee_attendance_today(self, **kwargs):
        temp = []
        try:
            user = request.env.user
            current_projects = request.env['project.project'].sudo().search([])
            if kwargs.get('project_id'):
                current_projects = request.env['project.project'].sudo().search([('id', '=', kwargs['project_id'])], limit=1)
            pl_employees = current_projects.mapped('project_lead')
            qc_employees = current_projects.mapped('project_qc_reviewer')
            tasker_employees = current_projects.mapped('project_tasker')

            # Create a combined unique list for the search domain
            all_target_employees = pl_employees | qc_employees | tasker_employees
            today = date.today()
            Attendance = request.env['hr.attendance'].sudo()
            attendance = Attendance.search([
                ('employee_id', 'in', all_target_employees),
                ('check_in', '>=', datetime.combine(today, datetime.min.time())),
                ('check_in', '<', datetime.combine(today, datetime.max.time())),
            ], limit=1)

            if not attendance:
                return return_Response(message="No attendance record for today", status=200, data={'data': None})
            for atte in attendance:
                temp.append(self._format_attendance(atte))
            return return_Response(
                message="Today's attendance",
                status=200,
                data={'record': temp,'count': len(temp)},
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

