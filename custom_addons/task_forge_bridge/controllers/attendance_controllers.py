from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
from datetime import datetime, date, timedelta
import json

from odoo import fields
class TaskForgeAttendanceController(http.Controller):

    @http.route('/api/v2/taskforge/attendance/punch_in', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({})
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
                if existing.attendance_status == 'present':
                    return return_Response(message="Already punched in today", status=400)
                else:
                    vals = {'attendance_status': 'present', 'check_in': datetime.now()}
                    if jdata.get('location'):
                        vals['geo_location'] = jdata['location']
                    if jdata.get('geo_coordinates'):
                        vals['geo_coordinates'] = jdata['geo_coordinates']
                    existing.write(vals)
                    return return_Response(
                        message="Punched in successfully",
                        status=200,
                        data={'data': {
                            'id': existing.id,
                            'employee_id': employee.id,
                            'employee_name': employee.name,
                            'date': str(today),
                            'punch_in_time': existing.check_in.isoformat() if existing.check_in else None,
                            'location': existing.geo_location or '',
                            'geo_coordinates': existing.geo_coordinates or '',
                            'status': 'Present',
                        }}
                    )

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
                ('attendance_status', '=', 'present')
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

            # Determine date range for filtering and stats
            date_param = kwargs.get('date')
            start_date_param = kwargs.get('start_date')
            end_date_param = kwargs.get('end_date')

            if date_param:
                # Single date filter: records for that day, stats for that month
                filter_date = datetime.strptime(date_param, '%Y-%m-%d').date()
                domain.append(('check_in', '>=', datetime.combine(filter_date, datetime.min.time())))
                domain.append(('check_in', '<', datetime.combine(filter_date + timedelta(days=1), datetime.min.time())))
                stats_start = filter_date.replace(day=1)
                next_month = (stats_start + timedelta(days=32)).replace(day=1)
                stats_end = next_month - timedelta(days=1)
            elif start_date_param or end_date_param:
                # Date range filter: records and stats within the range
                stats_start = datetime.strptime(start_date_param, '%Y-%m-%d').date() if start_date_param else None
                stats_end = datetime.strptime(end_date_param, '%Y-%m-%d').date() if end_date_param else None
                if stats_start:
                    domain.append(('check_in', '>=', datetime.combine(stats_start, datetime.min.time())))
                if stats_end:
                    domain.append(('check_in', '<', datetime.combine(stats_end + timedelta(days=1), datetime.min.time())))
            else:
                # No date filter: records unlimited, stats for current month
                today = date.today()
                stats_start = today.replace(day=1)
                stats_end = today
            if kwargs.get('status'):
                status = kwargs.get('status').split(',')
                status = ['present', 'on_leave', 'absent'] if "all" in status else status
                if status:
                    domain.append(('attendance_status', 'in', status))
            records = Attendance.search(domain, order='check_in desc', limit=200)
            data = [self._format_attendance(rec) for rec in records]

            # Compute summary stats over the stats date range
            emp_domain = [('employee_id', 'in', team_ids), ('attendance_status', '=', 'present')]
            if user_id_param:
                emp_domain.append(('employee_id', '=', int(user_id_param)))

            if date_param or (not start_date_param and not end_date_param):
                emp_domain.append(('check_in', '>=', datetime.combine(stats_start, datetime.min.time())))
                emp_domain.append(('check_in', '<', datetime.combine(stats_end + timedelta(days=1), datetime.min.time())))
            else:
                if stats_start:
                    emp_domain.append(('check_in', '>=', datetime.combine(stats_start, datetime.min.time())))
                if stats_end:
                    emp_domain.append(('check_in', '<', datetime.combine(stats_end + timedelta(days=1), datetime.min.time())))
            total_present_days = Attendance.search(emp_domain)
            total_hours = sum(total_present_days.mapped('worked_hours'))
            avg_working_hours = round(total_hours / len(total_present_days), 2) if total_present_days else 0

            # Count approved leave days in stats range
            Leave = request.env['hr.leave'].sudo()
            leave_domain = [
                ('employee_id', 'in', team_ids),
                ('state', '=', 'validate'),
            ]
            if user_id_param:
                leave_domain.append(('employee_id', '=', int(user_id_param)))
            if date_param or (not start_date_param and not end_date_param):
                leave_domain.append(('date_from', '<=', datetime.combine(stats_end, datetime.max.time())))
                leave_domain.append(('date_to', '>=', datetime.combine(stats_start, datetime.min.time())))
            else:
                if stats_start:
                    leave_domain.append(('date_to', '>=', datetime.combine(stats_start, datetime.min.time())))
                if stats_end:
                    leave_domain.append(('date_from', '<=', datetime.combine(stats_end, datetime.max.time())))

            leave_records = Leave.search(leave_domain)
            total_leave_days = sum(leave_records.mapped('number_of_days'))

            summary = {
                'total_present_days': len(total_present_days),
                'total_leave_days': total_leave_days,
                'avg_working_hours': avg_working_hours,
                'stats_start': str(stats_start) if stats_start else None,
                'stats_end': str(stats_end) if stats_end else None,
            }

            return return_Response(message="Attendance list", status=200, data={'data': data, 'summary': summary})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _format_attendance(self, rec):
        attendance_status = {
            'present': 'Present',
            'absent': 'Absent',
            'on_leave': 'Leave'
        }
        return {
            'id': rec.id if rec.id else 0,
            'employee_id': rec.employee_id.id if rec.employee_id.id else 0,
            'employee_name': rec.employee_id.name if rec.employee_id.name else "",
            'role': rec.employee_id.user_id.user_role.name if rec.employee_id.user_id.user_role.name else "",
            'date': str(rec.check_in.date()) if rec.check_in else '',
            'status': attendance_status.get(rec.attendance_status) if rec.attendance_status else "Absent",
            'punch_in_time': rec.check_in.isoformat() if rec.check_in else "",
            'punch_out_time': rec.check_out.isoformat() if rec.check_out else "",
            'hours_worked': round(rec.worked_hours, 2) if rec.worked_hours else 0,
            'location': rec.geo_location or '',
            'geo_coordinates': rec.geo_coordinates or '',
            'tasks_done': rec.tasks_done or 0,
        }

    @http.route('/api/v2/taskforge/all_employee_attendance/today', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def all_employee_attendance_today(self, **kwargs):
        temp = []
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)
            today = date.today()
            Attendance = request.env['hr.attendance'].sudo()
            domain = [('check_in', '>=', datetime.combine(today, datetime.min.time())), ('check_in', '<', datetime.combine(today, datetime.max.time()))]
            if kwargs.get('search', ''):
                search_key = kwargs.get('search', '').strip()
                if search_key:
                    domain.append(('employee_id.name', 'ilike', search_key))
            if kwargs.get('status'):
                status = kwargs.get('status').split(',')
                status = ['present', 'on_leave', 'absent'] if "all" in status else status
                if status:
                    domain.append(('attendance_status', 'in', status))
            attendance = Attendance.search(domain)
            for atte in attendance:
                temp.append(self._format_attendance(atte))
            return return_Response(
                message="Today's attendance",
                status=200,
                data={'data': temp,'count': len(temp)},
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

