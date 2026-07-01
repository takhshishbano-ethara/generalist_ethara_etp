from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
from datetime import datetime, date, timedelta
import json
import requests as http_requests
import logging

from odoo import fields

_logger = logging.getLogger(__name__)


def _reverse_geocode(geo_coordinates):
    """Convert 'lat,lng' to a human-readable location name using Nominatim."""
    if not geo_coordinates or ',' not in geo_coordinates:
        return ''
    try:
        parts = geo_coordinates.split(',')
        lat = parts[0].strip()
        lng = parts[1].strip()
        if not lat or not lng:
            return ''
        resp = http_requests.get(
            'https://nominatim.openstreetmap.org/reverse',
            params={'lat': lat, 'lon': lng, 'format': 'json', 'zoom': 10},
            headers={'User-Agent': 'EtharaETP/1.0'},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            address = data.get('address', {})
            city = address.get('city') or address.get('town') or address.get('village') or address.get('county') or ''
            state = address.get('state') or ''
            if city and state:
                return f"{city}, {state}"
            elif city:
                return city
            elif state:
                return state
            return data.get('display_name', '')[:100] if data.get('display_name') else ''
    except Exception as e:
        _logger.warning('Reverse geocoding failed for %s: %s', geo_coordinates, str(e))
    return ''
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
            # --- NEW LOGIC: RESTRICT PUNCH-IN IF ON LEAVE ---
            Leave = request.env['hr.leave'].sudo()
            approved_leave = Leave.search([
                ('employee_id', '=', employee.id),
                ('state', '=', 'validate'),
                ('request_date_from', '<=', today),
                ('request_date_to', '>=', today),
            ], limit=1)

            if approved_leave:
                return return_Response(
                    message=f"Punch-in restricted: You have an approved leave for today ({approved_leave.holiday_status_id.name}).",
                    status=400
                )
            # -----------------------------------------------
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
                    vals = {'attendance_status': 'present', 'check_in': datetime.now(), 'check_out': ""}
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

            Attendance.close_open_attendance_record()
            vals = {
                'employee_id': employee.id,
                'check_in': datetime.now(),
            }
            if jdata.get('location'):
                vals['geo_location'] = jdata['location']
            if jdata.get('geo_coordinates'):
                vals['geo_coordinates'] = jdata['geo_coordinates']

            attendance = Attendance.create(vals)
            IST_OFFSET = timedelta(hours=5, minutes=30)
            check_in_ist = (attendance.check_in + IST_OFFSET) if attendance.check_in else ""

            return return_Response(
                message="Punched in successfully",
                status=200,
                data={'data': {
                    'id': attendance.id,
                    'employee_id': employee.id,
                    'employee_name': employee.name,
                    'date': str(today),
                    'punch_in_time': str(check_in_ist),
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
            IST_OFFSET = timedelta(hours=5, minutes=30)
            check_in_ist = (attendance.check_in + IST_OFFSET) if attendance.check_in else ""
            check_out_ist = (attendance.check_out + IST_OFFSET) if attendance.check_out else ""

            return return_Response(
                message="Punched out successfully",
                status=200,
                data={'data': {
                    'id': attendance.id,
                    'employee_id': employee.id,
                    'employee_name': employee.name,
                    'date': str(today),
                    'punch_in_time': str(check_in_ist),
                    'punch_out_time': str(check_out_ist),
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
                data={'data': self._format_attendance(attendance, target_date=today)}
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

            team_ids = list(set((employee._get_team_employee_ids() or []) + [employee.id]))
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
            fmt_target = datetime.strptime(date_param, '%Y-%m-%d').date() if date_param else None
            data = [self._format_attendance(rec, target_date=fmt_target) for rec in records]

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

    def _format_attendance(self, rec, target_date=None):
        attendance_status = {
            'present': 'Present',
            'absent': 'Absent',
            'on_leave': 'Leave'
        }
        IST_OFFSET = timedelta(hours=5, minutes=30)
        ist_check_in_date = (rec.check_in + IST_OFFSET).date() if rec.check_in else None
        task_log_date = target_date or ist_check_in_date or date.today()
        task_logs = self.env['task.forge.log'].sudo().search_count([
            ('employee_id', '=', rec.employee_id.id),
            ('date', '=', task_log_date),
            ('state', '=', 'completed')
        ])
        IST_OFFSET = timedelta(hours=5, minutes=30)
        is_placeholder_absent = rec.attendance_status == 'absent' and not rec.check_out
        check_in_ist = (rec.check_in + IST_OFFSET) if rec.check_in and not is_placeholder_absent else ""
        check_out_ist = (rec.check_out + IST_OFFSET) if rec.check_out else ""

        biometric_in = ""
        biometric_out = ""
        biometric_location = ""
        biometric_hours = None
        if 'essl_pull_api.daily.summary' in self.env and rec.employee_id:
            biometric_date = target_date or ist_check_in_date or date.today()
            biometric = self.env['essl_pull_api.daily.summary'].sudo().search([
                ('employee_id', '=', rec.employee_id.id),
                ('punch_date', '=', biometric_date),
            ], limit=1)
            if biometric:
                biometric_in = biometric.punch_in_time or ""
                biometric_out = biometric.punch_out_time or ""
                biometric_location = getattr(biometric, 'location', '') or ''
                if biometric_in and biometric_out:
                    try:
                        bin_dt = datetime.strptime(biometric_in, '%Y-%m-%d %H:%M:%S')
                        bout_dt = datetime.strptime(biometric_out, '%Y-%m-%d %H:%M:%S')
                        if bout_dt > bin_dt:
                            biometric_hours = round((bout_dt - bin_dt).total_seconds() / 3600.0, 2)
                        else:
                            biometric_hours = 0
                    except Exception:
                        biometric_hours = None

        location = biometric_location or rec.geo_location or ''
        if not location and rec.geo_coordinates:
            location = _reverse_geocode(rec.geo_coordinates)
            if location:
                try:
                    rec.sudo().write({'geo_location': location})
                except Exception:
                    pass

        return {
            'id': rec.id if rec.id else 0,
            'employee_id': rec.employee_id.id if rec.employee_id.id else 0,
            'employee_name': rec.employee_id.name if rec.employee_id.name else "",
            'role': rec.employee_id.user_id.user_role.name if rec.employee_id.user_id.user_role.name else "",
            'date': str(rec.date) if rec.date else (str(check_in_ist.date()) if check_in_ist else (str(target_date) if target_date else '')),
            'status': attendance_status.get(rec.attendance_status) if rec.attendance_status else "Absent",
            'punch_in_time': biometric_in or str(check_in_ist),
            'punch_out_time': biometric_out or str(check_out_ist),
            'hours_worked': biometric_hours if biometric_hours is not None else (round(rec.worked_hours, 2) if rec.worked_hours else 0),
            'location': location,
            'geo_coordinates': rec.geo_coordinates or '',
            'tasks_done': task_logs or 0,
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
            team_ids = list(set((employee._get_team_employee_ids() or []) + [employee.id]))
            today = date.today()
            Attendance = request.env['hr.attendance'].sudo()
            domain = [('employee_id', 'in', team_ids)]

            date_param = kwargs.get('date')
            start_date_param = kwargs.get('start_date')
            end_date_param = kwargs.get('end_date')

            if date_param:
                filter_date = datetime.strptime(date_param, '%Y-%m-%d').date()
                domain.append(('check_in', '>=', datetime.combine(filter_date, datetime.min.time())))
                domain.append(('check_in', '<', datetime.combine(filter_date + timedelta(days=1), datetime.min.time())))
            elif start_date_param or end_date_param:
                stats_start = datetime.strptime(start_date_param, '%Y-%m-%d').date() if start_date_param else None
                stats_end = datetime.strptime(end_date_param, '%Y-%m-%d').date() if end_date_param else None
                if stats_start:
                    domain.append(('check_in', '>=', datetime.combine(stats_start, datetime.min.time())))
                if stats_end:
                    domain.append(('check_in', '<', datetime.combine(stats_end + timedelta(days=1), datetime.min.time())))
            else:
                domain.append(('check_in', '>=', datetime.combine(today, datetime.min.time())))
                domain.append(('check_in', '<', datetime.combine(today + timedelta(days=1), datetime.min.time())))

            if kwargs.get('search', ''):
                search_key = kwargs.get('search', '').strip()
                if search_key:
                    domain.append(('employee_id.name', 'ilike', search_key))

            if kwargs.get('status'):
                status = kwargs.get('status').split(',')
                status = ['present', 'on_leave', 'absent'] if "all" in status else status
                if status:
                    domain.append(('attendance_status', 'in', status))
            page = int(kwargs.get('page')) if kwargs.get('page') else 1
            limit = int(kwargs.get('limit')) if kwargs.get('limit') else 10
            offset = (page - 1) * limit
            total_count = request.env['hr.attendance'].sudo().search_count(domain)
            if not kwargs.get('page'):
                limit = total_count
                offset = 0

            attendance = Attendance.search(domain, limit=limit, offset=offset)
            if date_param:
                fmt_target = datetime.strptime(date_param, '%Y-%m-%d').date()
            elif start_date_param or end_date_param:
                fmt_target = None
            else:
                fmt_target = today
            for atte in attendance:
                temp.append(self._format_attendance(atte, target_date=fmt_target))

            return return_Response(
                message="Today's attendance",
                status=200,
                data={'data': temp,'count': len(temp), 'total_record_count': total_count},
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)


