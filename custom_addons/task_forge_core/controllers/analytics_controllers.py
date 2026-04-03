from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token
)
from datetime import datetime, date, timedelta
import json


class TaskForgeAnalyticsController(http.Controller):

    @http.route('/api/v2/taskforge/analytics/overview', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def analytics_overview(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()
            today = date.today()
            now = datetime.now()

            TaskLog = request.env['task.forge.log'].sudo()
            Attendance = request.env['hr.attendance'].sudo()
            Leave = request.env['hr.leave'].sudo()
            Flag = request.env['task.forge.flag'].sudo()
            Project = request.env['project.project'].sudo()
            Employee = request.env['hr.employee'].sudo()

            # Present today
            present_today = Attendance.search_count([
                ('employee_id', 'in', team_ids),
                ('check_in', '>=', datetime.combine(today, datetime.min.time())),
                ('check_in', '<', datetime.combine(today, datetime.max.time())),
            ])

            # Tasks completed today
            tasks_completed_today = TaskLog.search_count([
                ('employee_id', 'in', team_ids),
                ('date', '=', today),
                ('state', '=', 'completed'),
            ])

            # Live projects
            live_projects = Project.search_count([('task_forge_status', '=', 'live')])

            # Average AHT
            tasks_with_time = TaskLog.search([
                ('employee_id', 'in', team_ids),
                ('time_taken_mins', '>', 0),
            ])
            total_time = sum(tasks_with_time.mapped('time_taken_mins'))
            total_count = len(tasks_with_time)
            avg_aht = round(total_time / total_count, 1) if total_count else 0

            # Active taskers
            active_taskers = Employee.search_count([
                ('id', 'in', team_ids),
                ('task_forge_active', '=', True),
            ])

            # Pending leaves
            pending_leaves = Leave.search_count([
                ('employee_id', 'in', team_ids),
                ('state', '=', 'confirm'),
            ])

            # Unresolved flags
            unresolved_flags = Flag.search_count([('is_resolved', '=', False)])

            data = {
                'present_today': present_today,
                'tasks_completed_today': tasks_completed_today,
                'live_projects': live_projects,
                'avg_aht': avg_aht,
                'active_taskers': active_taskers,
                'pending_leaves': pending_leaves,
                'unresolved_flags': unresolved_flags,
            }

            return return_Response(message="Analytics overview", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/analytics/productivity', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def productivity(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()
            TaskLog = request.env['task.forge.log'].sudo()
            Employee = request.env['hr.employee'].sudo()

            result = []
            for emp in Employee.browse(team_ids):
                total = TaskLog.search_count([('employee_id', '=', emp.id)])
                completed = TaskLog.search_count([('employee_id', '=', emp.id), ('state', '=', 'completed')])
                tasks_with_time = TaskLog.search([
                    ('employee_id', '=', emp.id),
                    ('time_taken_mins', '>', 0),
                ])
                total_time = sum(tasks_with_time.mapped('time_taken_mins'))
                count = len(tasks_with_time)

                result.append({
                    'employee_id': emp.id,
                    'employee_name': emp.name,
                    'tasks_completed': completed,
                    'total_tasks': total,
                    'productivity': round((completed / total * 100) if total else 0, 1),
                    'aht': round(total_time / count, 1) if count else 0,
                })

            return return_Response(message="Productivity", status=200, data={'data': result})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/productivity/tasker', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def tasker_productivity(self, **kwargs):
        try:
            tasker_id = kwargs.get('employee_id')
            if not tasker_id:
                return return_Response(message="employee_id required", status=400)

            target_date = kwargs.get('date', str(date.today()))
            TaskLog = request.env['task.forge.log'].sudo()

            tasks = TaskLog.search([
                ('employee_id', '=', int(tasker_id)),
                ('date', '=', target_date),
            ])

            per_task = []
            per_project = {}
            for t in tasks:
                per_task.append({
                    'task_id': t.id,
                    'task_name': t.name,
                    'project_name': t.project_id.name if t.project_id else 'No Project',
                    'time_taken_mins': t.time_taken_mins,
                    'status': t.state,
                })
                proj_name = t.project_id.name if t.project_id else 'No Project'
                if proj_name not in per_project:
                    per_project[proj_name] = {'tasks': 0, 'total_mins': 0}
                per_project[proj_name]['tasks'] += 1
                per_project[proj_name]['total_mins'] += (t.time_taken_mins or 0)

            total = len(tasks)
            completed = len(tasks.filtered(lambda t: t.state == 'completed'))
            hours_logged = sum(tasks.mapped('time_taken_mins')) / 60 if tasks else 0

            data = {
                'date': target_date,
                'per_task': per_task,
                'per_project': [{'project': k, **v} for k, v in per_project.items()],
                'total_tasks': total,
                'completed': completed,
                'completion_rate': round((completed / total * 100) if total else 0, 1),
                'hours_logged': round(hours_logged, 2),
            }

            return return_Response(message="Tasker productivity", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/inactivity_check', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def inactivity_check(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            today = date.today()
            three_days_ago = today - timedelta(days=3)

            Employee = request.env['hr.employee'].sudo()
            Attendance = request.env['hr.attendance'].sudo()

            tasker_group = request.env.ref('etp_user_roles.group_tasker')
            active_taskers = Employee.search([
                ('task_forge_active', '=', True),
                ('user_id.groups_id', 'in', [tasker_group.id]),
            ])

            inactive = []
            for emp in active_taskers:
                recent = Attendance.search_count([
                    ('employee_id', '=', emp.id),
                    ('check_in', '>=', datetime.combine(three_days_ago, datetime.min.time())),
                ])
                if recent == 0:
                    inactive.append({
                        'employee_id': emp.id,
                        'employee_name': emp.name,
                        'pl_name': emp.task_forge_pl_id.name if emp.task_forge_pl_id else '',
                        'qr_name': emp.task_forge_qr_id.name if emp.task_forge_qr_id else '',
                    })

            return return_Response(message=f"{len(inactive)} inactive members", status=200, data={'data': inactive})
        except Exception as e:
            return return_Response(message=str(e), status=400)
