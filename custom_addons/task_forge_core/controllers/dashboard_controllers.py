from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token
)
from datetime import datetime, date, timedelta
import json


class TaskForgeDashboardController(http.Controller):

    @http.route('/api/v2/taskforge/dashboard/founder', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def founder_dashboard(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_cto'):
                return return_Response(message="Admin access required", status=403)

            today = date.today()
            Employee = request.env['hr.employee'].sudo()
            TaskLog = request.env['task.forge.log'].sudo()
            Attendance = request.env['hr.attendance'].sudo()
            Leave = request.env['hr.leave'].sudo()
            Project = request.env['project.project'].sudo()
            Blocker = request.env['task.forge.blocker'].sudo()

            all_active = Employee.search([('task_forge_active', '=', True)])
            all_ids = all_active.ids

            total_members = len(all_ids)

            present_today = Attendance.search_count([
                ('employee_id', 'in', all_ids),
                ('check_in', '>=', datetime.combine(today, datetime.min.time())),
                ('check_in', '<', datetime.combine(today, datetime.max.time())),
            ])

            on_leave_today = Leave.search_count([
                ('employee_id', 'in', all_ids),
                ('date_from', '<=', today),
                ('date_to', '>=', today),
                ('state', '=', 'validate'),
            ])

            tasks_completed_today = TaskLog.search_count([
                ('employee_id', 'in', all_ids),
                ('date', '=', today),
                ('state', '=', 'completed'),
            ])

            live_projects = Project.search_count(Project._task_forge_live_domain())

            open_blockers = Blocker.search_count([('state', 'in', ['pending', 'ack'])])

            pending_leaves = Leave.search_count([
                ('employee_id', 'in', all_ids),
                ('state', '=', 'confirm'),
            ])

            # Role distribution
            role_dist = {}
            for emp in all_active:
                role = emp._get_task_forge_role()
                role_dist[role] = role_dist.get(role, 0) + 1

            data = {
                'total_members': total_members,
                'present_today': present_today,
                'on_leave_today': on_leave_today,
                'tasks_completed_today': tasks_completed_today,
                'live_projects': live_projects,
                'open_blockers': open_blockers,
                'pending_leaves': pending_leaves,
                'role_distribution': role_dist,
            }

            return return_Response(message="Founder dashboard", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/dashboard/project_health', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def project_health(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            Project = request.env['project.project'].sudo()
            TaskLog = request.env['task.forge.log'].sudo()
            Blocker = request.env['task.forge.blocker'].sudo()

            projects = Project.search(Project._task_forge_live_domain())
            data = []

            for proj in projects:
                tasks = TaskLog.search([('project_id', '=', proj.id)])
                completed = len(tasks.filtered(lambda t: t.state == 'completed'))
                blockers = Blocker.search_count([('project_id', '=', proj.id), ('state', 'in', ['pending', 'ack'])])
                overdue = len(tasks.filtered(lambda t: t.state == 'overdue'))
                total = len(tasks)

                # Health status
                if blockers > 5 or overdue > 3:
                    health = 'at_risk'
                elif blockers > 2 or overdue > 1:
                    health = 'warning'
                else:
                    health = 'healthy'

                data.append({
                    'project_id': proj.id,
                    'project_name': proj.name,
                    'total_tasks': total,
                    'completed': completed,
                    'pending': total - completed,
                    'blockers': blockers,
                    'overdue': overdue,
                    'health': health,
                })

            return return_Response(message="Project health", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/dashboard/rankings', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def productivity_rankings(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            Employee = request.env['hr.employee'].sudo()
            TaskLog = request.env['task.forge.log'].sudo()

            tasker_group = request.env.ref('etp_user_roles.group_tasker')
            taskers = Employee.search([
                ('task_forge_active', '=', True),
                # Odoo 19: res.users.groups_id → all_group_ids for the
                # full (implied-inclusive) membership set.
                ('user_id.all_group_ids', 'in', [tasker_group.id]),
            ])

            rankings = []
            for emp in taskers:
                total = TaskLog.search_count([('employee_id', '=', emp.id)])
                completed = TaskLog.search_count([('employee_id', '=', emp.id), ('state', '=', 'completed')])
                productivity = round((completed / total * 100) if total else 0, 1)
                rankings.append({
                    'employee_id': emp.id,
                    'employee_name': emp.name,
                    'total_tasks': total,
                    'completed': completed,
                    'productivity': productivity,
                })

            rankings.sort(key=lambda x: x['productivity'], reverse=True)

            top_taskers = rankings[:10]
            low_performers = [r for r in rankings if r['productivity'] < 50]

            data = {
                'top_taskers': top_taskers,
                'low_performers': low_performers,
            }

            return return_Response(message="Rankings", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/dashboard/leave_impact', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def leave_impact(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            today = date.today()
            Employee = request.env['hr.employee'].sudo()
            Leave = request.env['hr.leave'].sudo()

            on_leave = Leave.search([
                ('date_from', '<=', today),
                ('date_to', '>=', today),
                ('state', '=', 'validate'),
            ])
            on_leave_emps = on_leave.mapped('employee_id')

            affected_pls = set()
            affected_qrs = set()
            for emp in on_leave_emps:
                if emp.task_forge_pl_id:
                    affected_pls.add(emp.task_forge_pl_id.name)
                if emp.task_forge_qr_id:
                    affected_qrs.add(emp.task_forge_qr_id.name)

            pending_approvals = Leave.search_count([('state', '=', 'confirm')])

            data = {
                'on_leave_today': len(on_leave_emps),
                'affected_pls': list(affected_pls),
                'affected_qrs': list(affected_qrs),
                'pending_approvals': pending_approvals,
            }

            return return_Response(message="Leave impact", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/dashboard/risk_quality', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def risk_quality(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            Blocker = request.env['task.forge.blocker'].sudo()
            TaskLog = request.env['task.forge.log'].sudo()
            Attendance = request.env['hr.attendance'].sudo()
            Employee = request.env['hr.employee'].sudo()

            today = date.today()
            three_days_ago = today - timedelta(days=3)

            open_blockers = Blocker.search_count([('state', 'in', ['pending', 'ack'])])
            overdue_tasks = TaskLog.search_count([('state', '=', 'overdue')])

            # Low hours members (worked < 4 hours today)
            all_active = Employee.search([('task_forge_active', '=', True)])
            low_hours = []
            for emp in all_active:
                att = Attendance.search([
                    ('employee_id', '=', emp.id),
                    ('check_in', '>=', datetime.combine(today, datetime.min.time())),
                    ('check_in', '<', datetime.combine(today, datetime.max.time())),
                ], limit=1)
                if att and att.worked_hours and att.worked_hours < 4:
                    low_hours.append({
                        'employee_id': emp.id,
                        'employee_name': emp.name,
                        'hours_worked': round(att.worked_hours, 2),
                    })

            data = {
                'open_blockers': open_blockers,
                'overdue_tasks': overdue_tasks,
                'low_hours_members': low_hours,
            }

            return return_Response(message="Risk quality", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/dashboard/qr_analytics', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def qr_analytics(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            if not user.has_group('etp_user_roles.group_quality_reviewer'):
                return return_Response(message="QR role required", status=403)

            today = date.today()
            Employee = request.env['hr.employee'].sudo()
            TaskLog = request.env['task.forge.log'].sudo()
            Attendance = request.env['hr.attendance'].sudo()

            taskers = Employee.search([
                ('task_forge_qr_id', '=', employee.id),
                ('task_forge_active', '=', True),
            ])

            team_size = len(taskers)
            present_count = 0
            tasker_data = []

            for t in taskers:
                att = Attendance.search([
                    ('employee_id', '=', t.id),
                    ('check_in', '>=', datetime.combine(today, datetime.min.time())),
                    ('check_in', '<', datetime.combine(today, datetime.max.time())),
                ], limit=1)
                if att:
                    present_count += 1

                total = TaskLog.search_count([('employee_id', '=', t.id)])
                completed = TaskLog.search_count([('employee_id', '=', t.id), ('state', '=', 'completed')])

                tasker_data.append({
                    'employee_id': t.id,
                    'employee_name': t.name,
                    'total_tasks': total,
                    'completed': completed,
                    'productivity': round((completed / total * 100) if total else 0, 1),
                    'present_today': bool(att),
                })

            total_all = sum(d['total_tasks'] for d in tasker_data)
            completed_all = sum(d['completed'] for d in tasker_data)

            data = {
                'team_size': team_size,
                'present_today': present_count,
                'completion_rate': round((completed_all / total_all * 100) if total_all else 0, 1),
                'taskers': tasker_data,
            }

            return return_Response(message="QR analytics", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/hierarchy/performance', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def hierarchy_performance(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_cto'):
                return return_Response(message="Admin access required", status=403)

            Employee = request.env['hr.employee'].sudo()
            TaskLog = request.env['task.forge.log'].sudo()

            pl_group = request.env.ref('etp_user_roles.group_project_lead')
            pls = Employee.search([
                # Odoo 19: res.users.groups_id → all_group_ids for the
                # full (implied-inclusive) membership set.
                ('user_id.all_group_ids', 'in', [pl_group.id]),
                ('task_forge_active', '=', True),
            ])

            hierarchy = []
            for pl in pls:
                qrs = Employee.search([('task_forge_pl_id', '=', pl.id), ('task_forge_active', '=', True)])
                qr_data = []
                for qr in qrs:
                    taskers = Employee.search([('task_forge_qr_id', '=', qr.id), ('task_forge_active', '=', True)])
                    tasker_data = []
                    for t in taskers:
                        total = TaskLog.search_count([('employee_id', '=', t.id)])
                        completed = TaskLog.search_count([('employee_id', '=', t.id), ('state', '=', 'completed')])
                        tasker_data.append({
                            'id': t.id,
                            'name': t.name,
                            'total_tasks': total,
                            'completed': completed,
                            'productivity': round((completed / total * 100) if total else 0, 1),
                        })
                    qr_data.append({
                        'id': qr.id,
                        'name': qr.name,
                        'taskers': tasker_data,
                    })
                hierarchy.append({
                    'id': pl.id,
                    'name': pl.name,
                    'qrs': qr_data,
                })

            return return_Response(message="Hierarchy performance", status=200, data={'data': hierarchy})
        except Exception as e:
            return return_Response(message=str(e), status=400)
