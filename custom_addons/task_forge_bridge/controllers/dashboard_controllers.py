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
            escalated_blockers = request.env['task.forge.log'].sudo().search_count([('project_id', 'in', current_projects.ids), ('state', 'in', ['pending', 'ack'])])

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
                        'status': 'Active',
                    })
            return return_Response(message="Success", status=200, data={"records": temp, "count": len(temp)})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
