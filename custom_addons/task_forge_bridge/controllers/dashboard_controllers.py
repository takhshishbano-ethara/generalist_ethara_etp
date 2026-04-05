import datetime

from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request, safe_get_value
)
class DashboardController(http.Controller):

    @validate_token
    @http.route('/api/v2/get_cto_dashboard_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def v2_get_cto_dashboard_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            priority = {
                '0': 'Low',
                '1': 'Medium',
                '2': 'High',
                '3': 'Critical'
            }
            now = datetime.datetime.now()

            domain = []
            if kwargs.get('project_type'):
                domain.append(('y_project_type', '=', kwargs.get('project_type')))

            if kwargs.get('start_date') and kwargs.get('end_date'):
                if kwargs['start_date'] == kwargs['end_date']:
                    domain.append(('date_start', '=', kwargs['start_date']))
                else:
                    domain.append(('date_start', '>=', kwargs['start_date']))
                    domain.append(('date', '<=', kwargs['end_date']))

            current_projects = request.env['project.project'].sudo().search(domain)
            total_members = []
            pl_count = []
            qc_count = []
            tasker_count = []
            for project in current_projects:
                total_members.extend(project.project_lead.ids)
                pl_count.extend(project.project_lead.ids)
                total_members.extend(project.project_tasker.ids)
                tasker_count.extend(project.project_tasker.ids)
                total_members.extend(project.project_qc_reviewer.ids)
                qc_count.extend(project.project_qc_reviewer.ids)
            present_today = request.env['hr.attendance'].sudo().search([('check_in', '>=', f"{datetime.datetime.now().date()} 00:00:00"), ('employee_id', 'in', total_members)])
            present_yesterday_today = request.env['hr.attendance'].sudo().search_count([('check_in', '>=', f"{datetime.datetime.now().date() - datetime.timedelta(days=1)} 00:00:00"), ('check_out', '<=', f"{datetime.datetime.now().date() - datetime.timedelta(days=1)} 23:59:00"), ('employee_id', 'in', total_members)])
            on_leave_count = request.env['hr.leave'].sudo().search_count([
                ('employee_id', 'in', total_members),
                ('state', '=', 'validate'),
                ('date_from', '<=', datetime.datetime.now().date()),
                ('date_to', '>=', datetime.datetime.now().date())
            ])
            pending_leave_count = request.env['hr.leave'].sudo().search_count([
                            ('employee_id', 'in', total_members),
                            ('state', '=', 'confirm'),
                            ('date_from', '<=', datetime.datetime.now().date()),
                            ('date_to', '>=', datetime.datetime.now().date())
                        ])

            complete_task_count = request.env['task.forge.log'].sudo().search_count([('project_id', 'in', current_projects.ids), ('state', 'in', ['completed'])])

            blockers = request.env['task.forge.blocker'].sudo().read_group(
                domain=[('state', 'not in', ['no_issue'])],
                fields=['priority'],
                groupby=['priority']
            )
            blockers_count = request.env['task.forge.blocker'].sudo().search_count(domain=[('state', 'not in', ['no_issue'])])
            blockers_info = ", ".join([f"{block['priority_count']} {priority[block['priority']]}" for block in blockers])
            diff_percent = 0.0

            if present_yesterday_today > 0:
                diff_percent = ((len(present_today) - present_yesterday_today) / present_yesterday_today) * 100
            elif len(present_today) > 0:
                diff_percent = 100.0

            data = request.env['task.forge.log'].sudo().read_group(
                domain=[('project_id', 'in', current_projects.ids), ('state', 'in', ['completed']), ('end_time', '!=', False)],
                fields=['end_time', 'name'],
                groupby=['end_time:day']
            )
            labels = []
            values = []

            for line in data:
                labels.append(line['end_time:day'])
                values.append(line['end_time_count'])
            vals = {
                'total_member':{
                    'total_member_count': len(set(total_members)),
                    'total_pl_count': len(set(pl_count)),
                    'total_qc_count': len(set(qc_count)),
                    'total_tasker_count': len(set(tasker_count)),
                    'total_present_pl_count': len(present_today.filtered(lambda x: x.employee_id in pl_count)),
                    'total_present_qc_count': len(present_today.filtered(lambda x: x.employee_id in qc_count)),
                    'total_present_tasker_count': len(present_today.filtered(lambda x: x.employee_id in tasker_count))
                },
                'present_today': {
                    'present_employee_count': len(present_today),
                    'present_yesterday_count': present_yesterday_today,
                    'difference_percentage': round(diff_percent, 2)
                },
                'pending_leave_count':{
                    'pending_leave_count':  pending_leave_count
                },
                'on_leave':{
                    'on_leave_count':  on_leave_count
                },
                'task_completed':{
                    'total_done_task': complete_task_count
                },
                'open_blockers':{
                    'open_blockers_count': blockers_count,
                    'blocker_info': blockers_info
                },
                'task_completion_graph': {
                    'days': labels,
                    'total_completed': values,
                }
            }
            return return_Response(message="Success", status=200, data={"records": vals})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_pl_dashboard_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def v2_get_pl_dashboard_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)

            domain = [('project_lead', '=', user_id.employee_id.id)]
            if kwargs.get('project_type'):
                domain.append(('y_project_type', '=', kwargs.get('project_type')))

            if kwargs.get('start_date') and kwargs.get('end_date'):
                if kwargs['start_date'] == kwargs['end_date']:
                    domain.append(('date_start', '=', kwargs['start_date']))
                else:
                    domain.append(('date_start', '>=', kwargs['start_date']))
                    domain.append(('date', '<=', kwargs['end_date']))

            current_projects = request.env['project.project'].sudo().search(domain)
            total_task = request.env['task.forge.log'].sudo().search_count([('project_id', 'in', current_projects.ids)])

            total_members = []
            for project in current_projects:
                total_members.extend(project.project_tasker.ids)
                total_members.extend(project.project_qc_reviewer.ids)

            pending_leave_count = request.env['hr.leave'].sudo().search_count([
                ('employee_id', 'in', total_members),
                ('state', '=', 'confirm'),
                ('date_from', '<=', datetime.datetime.now().date()),
                ('date_to', '>=', datetime.datetime.now().date())
            ])
            escalated_task_count = request.env['task.forge.blocker'].sudo().search_count([('project_id', 'in', current_projects.ids), ('state', 'in', ['escalated'])])

            data = request.env['task.forge.log'].sudo().read_group(
                domain=[('project_id', 'in', current_projects.ids), ('state', 'in', ['completed']), ('end_time', '!=', False)],
                fields=['end_time', 'name'],
                groupby=['end_time:day']
            )
            labels = []
            values = []

            for line in data:
                labels.append(line['end_time:day'])
                values.append(line['end_time_count'])
            vals = {
                'total_task':{
                  'total_task_count': total_task,
                    'active_projects': len(current_projects),
                },
                'active_project':{
                  'active_project_count': len(current_projects),
                },
                'escalated_task':{
                  'escalated_task_count': escalated_task_count,
                },
                'pending_leave_count':{
                    'pending_leave_count':  pending_leave_count
                },
                'task_completion_graph': {
                    'days': labels,
                    'total_completed': values,
                }
            }
            return return_Response(message="Success", status=200, data={"records": vals})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_pl_dashboard_active_tasker_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_pl_dashboard_active_tasker_list(self, **kwargs):
        temp = []
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)

            domain = [('project_lead', '=', user_id.employee_id.id)]
            if kwargs.get('project_type'):
                domain.append(('y_project_type', '=', kwargs.get('project_type')))

            if kwargs.get('start_date') and kwargs.get('end_date'):
                if kwargs['start_date'] == kwargs['end_date']:
                    domain.append(('date_start', '=', kwargs['start_date']))
                else:
                    domain.append(('date_start', '>=', kwargs['start_date']))
                    domain.append(('date', '<=', kwargs['end_date']))

            current_projects = request.env['project.project'].sudo().search(domain)
            for project in current_projects:
                for emp in project.project_tasker:
                    temp.append({
                        'name':emp.name if emp.name else "",
                        'project_name':project.name if project.name else "",
                        'qr_name':emp.task_forge_qr_id.name if emp.task_forge_qr_id.name else "",
                        'status': 'Active' if request.env['task.forge.log'].sudo().search_count([('state', 'in', ['in_progress'])]) else "Idle"
                    })
            return return_Response(message="Success", status=200, data={"records": temp, "count": len(temp)})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_qc_dashboard_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_qc_dashboard_list(self, **kwargs):
        try:
            from datetime import datetime, date, time
            # 1. Use the authenticated user from the token (provided by @validate_token)
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            # 2. Build Project Domain
            project_domain = [('project_qc_reviewer', '=', employee.id)]
            if kwargs.get('project_type'):
                project_domain.append(('y_project_type', '=', kwargs.get('project_type')))

            # Date handling for projects
            if kwargs.get('start_date') and kwargs.get('end_date'):
                project_domain.extend([
                    ('date_start', '>=', kwargs['start_date']),
                    ('date_start', '<=', kwargs['end_date'])
                ])

            current_projects = request.env['project.project'].sudo().search(project_domain)

            # 3. Tasker Stats (Working vs Idle)
            # Find all taskers assigned to this QC Reviewer
            total_tasker = request.env['hr.employee'].sudo().search([('task_forge_qr_id', '=', employee.id)])

            # Find logs for these taskers that are currently 'in_progress'
            working_logs = request.env['task.forge.log'].sudo().search([
                ('employee_id', 'in', total_tasker.ids),
                ('state', '=', 'in_progress')
            ])

            # Get unique employees who are actually working
            working_tasker_ids = working_logs.mapped('employee_id')
            idle_tasker = total_tasker - working_tasker_ids

            # 4. Daily Task Counts
            today_start = datetime.combine(date.today(), time.min)
            today_end = datetime.combine(date.today(), time.max)

            task_done_today = request.env['task.forge.log'].sudo().search_count([
                ('project_id', 'in', current_projects.ids),
                ('end_time', '>=', today_start),
                ('end_time', '<=', today_end),
                ('state', '=', 'completed')
            ])

            # 5. Pending Items
            pending_blocker_count = request.env['task.forge.blocker'].sudo().search_count([
                ('project_id', 'in', current_projects.ids),
                ('state', '=', 'pending')
            ])

            pending_leave_count = request.env['hr.leave'].sudo().search_count([
                ('employee_id', 'in', total_tasker.ids),
                ('state', '=', 'confirm'),
                ('date_from', '<=', date.today()),
                ('date_to', '>=', date.today())
            ])

            # 6. Throughput Graph Data
            graph_data = request.env['task.forge.log'].sudo().read_group(
                domain=[('project_id', 'in', current_projects.ids), ('state', '=', 'completed')],
                fields=['end_time', 'name'],
                groupby=['end_time:day']
            )

            labels = [line['end_time:day'] for line in graph_data]
            values = [line['end_time_count'] for line in graph_data]

            vals = {
                'my_tasker': {
                    'total_count': len(total_tasker),
                    'working_count': len(working_tasker_ids),
                    'idle_count': len(idle_tasker),
                },
                'task_today': {
                    'task_done_today': task_done_today,
                },
                'pending_blocker': {
                    'count': pending_blocker_count,
                },
                'pending_leave': {
                    'count': pending_leave_count
                },
                'review_throughput_graph': {
                    'days': labels,
                    'total_completed': values,
                }
            }
            return return_Response(message="Success", status=200, data=vals)

        except Exception as e:
            return return_Response(message="Dashboard Load Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_qc_dashboard_tasker_performance_breakdown', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_qc_dashboard_tasker_performance_breakdown(self, **kwargs):
        temp = []
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)

            domain = [('project_qc_reviewer', '=', user_id.employee_id.id)]
            if kwargs.get('project_type'):
                domain.append(('y_project_type', '=', kwargs.get('project_type')))

            if kwargs.get('start_date') and kwargs.get('end_date'):
                if kwargs['start_date'] == kwargs['end_date']:
                    domain.append(('date_start', '=', kwargs['start_date']))
                else:
                    domain.append(('date_start', '>=', kwargs['start_date']))
                    domain.append(('date', '<=', kwargs['end_date']))
            current_projects = request.env['project.project'].sudo().search(domain)
            total_tasker = request.env['hr.employee'].sudo().search([('task_forge_qr_id', '=', user_id.employee_id.id)])
            attendance = request.env['hr.attendance'].sudo().search([('check_in', '>=', f"{datetime.datetime.now().date()} 00:00:00"), ('check_in', '<', f"{datetime.datetime.now().date()} 23:59:00")])
            present_employees = attendance.mapped('employee_id')
            for tasker in total_tasker:
                total_task = request.env['task.forge.log'].sudo().search([('project_id', 'in', current_projects.ids)])
                total_count = len(total_task)
                done_task = total_task.filtered(lambda t: t.state == 'completed')
                done_count = len(done_task)
                today_start = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
                today_end = datetime.datetime.combine(datetime.date.today(), datetime.time.max)
                today_task = total_task.filtered(lambda t: t.end_time and today_start <= t.end_time <= today_end)
                avg_hours_time = 0.0
                if total_count > 0:
                    avg_hours_time = sum(total_task.mapped('time_taken_mins')) / total_count
                completion_percent = 0.0
                if total_count > 0:
                    completion_percent = (done_count / total_count) * 100
                else:
                    completion_percent = 0.0

                temp.append({
                    'id': tasker.id if tasker else 0,
                    'name': tasker.name if tasker.name else "",
                    'status': "Present" if tasker in present_employees else "Absent",
                    'total_task': total_count,
                    'done_task': done_count,
                    'today_task': len(today_task),
                    'quality': completion_percent,
                    'completion': completion_percent,
                    'aht': avg_hours_time,
                })
            return return_Response(message="Success", status=200, data={"records": temp, "count": len(temp)})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_tasker_project_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_tasker_project_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)

            domain = [('project_tasker', '=', user_id.employee_id.id)]
            search = kwargs.get('search')
            if search:
                domain += ['|', ('name', 'ilike', search), ('internal_project_name', 'ilike', search)]
            projects = request.env['project.project'].sudo().search(domain)
            project_data = []
            for p in projects:
                team_ids = (p.project_lead.ids + p.project_aire.ids + p.project_swe.ids)
                unique_team_count = len(set(team_ids))
                project_data.append({
                    'id': safe_get_value(p, 'id', 'int'),
                    'project_name': safe_get_value(p, 'name', 'str'),
                    'project_id_code': safe_get_value(p, 'project_seq', 'str'),
                    'client': safe_get_value(p, 'client_name', 'str'),
                    'status': safe_get_value(p, 'stage_id.name', 'str'),
                    'progress': 0,
                    'tasks': safe_get_value(p, 'sample_task_number', 'int'),
                    'team_count': unique_team_count,
                    'blockers': 0,
                    'category': safe_get_value(p, 'project_category', 'str'),
                    'type': safe_get_value(p, 'project_type', 'str'),
                    'date_start': safe_get_value(p, 'date_start', 'date'),
                    'date_end': safe_get_value(p, 'date', 'date'),
                })
            return return_Response(
                message="Success",
                status=200,
                data={"record": project_data, "total_record_count": len(project_data), "count": len(project_data)})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_employee_project_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_employee_project_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)
            domain = []
            if user_id.user_role.id in [request.env.ref('api_auth_gateway.role_pl_technical').id, request.env.ref('api_auth_gateway.role_pl_stem').id, request.env.ref('api_auth_gateway.role_pl_non_stem').id]:
                domain = [('project_lead', '=', user_id.employee_id.id)]
            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_qc_technical').id, request.env.ref('api_auth_gateway.role_qc_stem').id, request.env.ref('api_auth_gateway.role_qc_non_stem').id]:
                domain = [('project_qc_reviewer', '=', user_id.employee_id.id)]
            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_tasker_technical').id, request.env.ref('api_auth_gateway.role_tasker_stem').id, request.env.ref('api_auth_gateway.role_tasker_non_stem').id]:
                domain = [('project_tasker', '=', user_id.employee_id.id)]
            search = kwargs.get('search')
            if search:
                domain += ['|', ('name', 'ilike', search), ('internal_project_name', 'ilike', search)]
            projects = request.env['project.project'].sudo().search(domain)
            project_data = []
            for p in projects:
                team_ids = (p.project_lead.ids + p.project_aire.ids + p.project_swe.ids)
                unique_team_count = len(set(team_ids))
                project_data.append({
                    'id': safe_get_value(p, 'id', 'int'),
                    'project_name': safe_get_value(p, 'name', 'str'),
                    'project_id_code': safe_get_value(p, 'project_seq', 'str'),
                    'client': safe_get_value(p, 'client_name', 'str'),
                    'status': safe_get_value(p, 'stage_id.name', 'str'),
                    'progress': 0,
                    'tasks': safe_get_value(p, 'sample_task_number', 'int'),
                    'team_count': unique_team_count,
                    'blockers': 0,
                    'category': safe_get_value(p, 'project_category', 'str'),
                    'type': safe_get_value(p, 'project_type', 'str'),
                    'date_start': safe_get_value(p, 'date_start', 'date'),
                    'date_end': safe_get_value(p, 'date', 'date'),
                })
            return return_Response(
                message="Success",
                status=200,
                data={"record": project_data, "total_record_count": len(project_data), "count": len(project_data)})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

