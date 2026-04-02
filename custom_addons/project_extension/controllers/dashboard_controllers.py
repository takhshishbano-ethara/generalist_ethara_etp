import datetime

from odoo import http
from odoo.http import request
from .utility import validate_request, validate_token, return_Response, safe_get_value

class DashboardController(http.Controller):

    @validate_token
    @http.route('/api/v1/get_cto_dashboard_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_cto_dashboard_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            priority = {
                '0': 'Low',
                '1': 'Medium',
                '2': 'High',
                '3': 'Critical'
            }
            now = datetime.datetime.now()
            last_month = now - datetime.timedelta(days=30)

            domain = []
            if kwargs.get('start_date') and kwargs.get('end_date'):
                if kwargs['start_date'] == kwargs['end_date']:
                    domain.append(('date_start', '=', kwargs['start_date']))
                else:
                    domain.append(('date_start', '>=', kwargs['start_date']))
                    domain.append(('date', '<=', kwargs['end_date']))

            current_projects = request.env['project.project'].sudo().search_count(domain)
            last_month_domain = domain + [('create_date', '<', last_month.strftime('%Y-%m-%d 00:00:00'))]
            last_month_count = request.env['project.project'].sudo().search_count(last_month_domain)

            diff_percent = 0
            if last_month_count > 0:
                diff_percent = round(((current_projects - last_month_count) / last_month_count) * 100, 2)

            total_count = request.env['project.project'].sudo().search_count(domain)
            blockers = request.env['project.blocker'].sudo().read_group(
                domain=[('state', 'in', ['raised', 'in_progress', 'testing'])],
                fields=['priority'],
                groupby=['priority']
            )
            blockers_count = request.env['project.blocker'].sudo().search_count(domain=[('state', 'in', ['raised', 'in_progress', 'testing'])])
            completed_projects = request.env['project.project'].sudo().search_count(
                domain + [('stage_id', '=', request.env.ref('project_extension.project_project_stage_ethara_13').id)]
            )
            approval_rate = round((completed_projects / total_count * 100), 2) if total_count > 0 else 0.0
            blockers_info = ", ".join([f"{block['priority_count']} {priority[block['priority']]}" for block in blockers])
            vals = {
                'active_project':{
                    'active_project_count': total_count,
                    'project_difference_percentage': diff_percent,
                    'last_month_project_count': last_month_count
                },
                'pending_deliveries': {
                    'pending_deliveries_count': request.env['project.project'].sudo().search_count(domain + [('stage_id', '=', request.env.ref('project_extension.project_project_stage_ethara_10').id)]),
                    'pending_deliveries_waiting_client_reviews': request.env['project.project'].sudo().search_count(domain + [('stage_id', '=', request.env.ref('project_extension.project_project_stage_ethara_11').id)]),
                    'pending_deliveries_rework': request.env['project.project'].sudo().search_count(domain + [('stage_id', '=', request.env.ref('project_extension.project_project_stage_ethara_12').id)])
                },
                'org_approval':{
                    'org_approval_rate': approval_rate,
                    'org_approval_target': 85.0
                },
                'open_blockers':{
                    'open_blockers_count': blockers_count,
                    'blocker_info': blockers_info
                },
                'project_phase_graph': [{
                        'stage': p['stage_id'][1] if p['stage_id'] else "Undefined",
                        'count': p['stage_id_count'],
                        'percentage': round((p['stage_id_count'] / total_count) * 100, 2)
                    } for p in request.env['project.project'].sudo().read_group(
                domain=domain,
                fields=['stage_id'],
                groupby=['stage_id']
            )],
                'upcoming_deadlines': {
                    'today': [],
                    'tomorrow': [],
                    'this_week': []
                }
            }
            today_date = now.date()

            # 1. Fetch blocker counts as before
            blocker_data = request.env['project.blocker'].sudo().read_group(
                domain=[('state', 'in', ['raised', 'in_progress', 'testing'])],
                fields=['project_id'],
                groupby=['project_id']
            )
            blocker_counts = {item['project_id'][0]: item['project_id_count'] for item in blocker_data if
                              item['project_id']}

            def get_deadline_info(start_date, end_date):
                projects = request.env['project.project'].sudo().search([
                    ('date', '>=', f"{start_date} 00:00:00"),
                    ('date', '<=', f"{end_date} 23:59:59")
                ])

                results = []
                for pro in projects:
                    remaining_time = pro.date - now.date()
                    total_seconds = remaining_time.total_seconds()

                    if total_seconds < 0:
                        due_label = "Overdue"
                    elif total_seconds >= 86400:
                        days = int(total_seconds // 86400)
                        due_label = f"In {days} day{'s' if days > 1 else ''}"
                    else:
                        hours = int(total_seconds // 3600)
                        due_label = f"In {hours} hour{'s' if hours > 1 else ''}"

                    count = blocker_counts.get(pro.id, 0)
                    if count > 0:
                        message = f"{count} blockers are pending"
                    else:
                        stage_name = pro.stage_id.name or 'Initial'
                        message = f"{stage_name} stage is due"

                    results.append({
                        'name': pro.name,
                        'message': message,
                        'due_in': due_label,
                        'status': 'blocked' if count > 0 else 'clear'
                    })
                return results

            vals["upcoming_deadlines"] = {
                "today": get_deadline_info(today_date, today_date),
                "tomorrow": get_deadline_info(today_date + datetime.timedelta(days=1), today_date + datetime.timedelta(days=1)),
                "this_week": get_deadline_info(today_date + datetime.timedelta(days=2), today_date + datetime.timedelta(days=7))
            }
            return return_Response(message="Success", status=200, data={"records": vals})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_project_blockers', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_project_blockers(self, **kwargs):
        try:
            domain = []
            if kwargs.get('project_id'):
                domain.append(('project_id', '=', int(kwargs.get('project_id'))))
            if kwargs.get('task_id'):
                domain.append(('task_id', '=', int(kwargs.get('task_id'))))

            state_param = kwargs.get('state')
            if state_param:
                domain.append(('state', '=', state_param))
            else:
                domain.append(('state', 'not in', ['resolved', 'cancelled']))

            blockers = request.env['project.blocker'].sudo().search(domain)
            blocker_list = []
            now = datetime.datetime.now()
            for blocker in blockers:
                remaining_str = ""
                if blocker.next_escalation_date:
                    diff = blocker.next_escalation_date - now
                    hours, remainder = divmod(diff.total_seconds(), 3600)
                    minutes, _ = divmod(remainder, 60)
                    remaining_str = f"{int(hours)}h {int(minutes)}m" if diff.total_seconds() > 0 else "Expired"

                blocker_list.append({
                    "priority_label": dict(blocker._fields['priority'].selection).get(blocker.priority, ''),
                    "remaining_time": remaining_str,
                    "id": safe_get_value(blocker, 'id', 'int'),
                    "name": safe_get_value(blocker, 'name', 'str'),
                    "state": safe_get_value(blocker, 'state', 'str'),
                    "state_label": dict(blocker._fields['state'].selection).get(blocker.state) if blocker.state else '',
                    "priority": safe_get_value(blocker, 'priority', 'str'),
                    "project_id": safe_get_value(blocker, 'project_id.id', 'int'),
                    "project_name": safe_get_value(blocker, 'project_id.name', 'str'),
                    "task_id": safe_get_value(blocker, 'task_id.id', 'int'),
                    "task_name": safe_get_value(blocker, 'task_id.name', 'str'),
                    "raised_by": safe_get_value(blocker, 'employee_id.name', 'str'),
                    "escalation_level": safe_get_value(blocker, 'escalation_level', 'str'),
                    "next_deadline": safe_get_value(blocker, 'next_escalation_date', 'str'),
                    "assigned_to": blocker.assigned_employee_ids.mapped('name') if blocker.assigned_employee_ids else []
                })

            return return_Response(
                message="Blockers retrieved successfully",
                status=200,
                data={"blockers": blocker_list, "count": len(blocker_list)}
            )

        except Exception as e:
            return return_Response(message="Fetch Failed", status=500, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_pl_dashboard_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_pl_dashboard_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)

            now = datetime.datetime.now()
            today_date = now.date()
            priority = {
                '0': 'Low',
                '1': 'Medium',
                '2': 'High',
                '3': 'Critical'
            }

            domain = [('project_lead', '=', user_id.employee_id.id)]

            if kwargs.get('start_date') and kwargs.get('end_date'):
                if kwargs['start_date'] == kwargs['end_date']:
                    domain.append(('date_start', '=', kwargs['start_date']))
                else:
                    domain.append(('date_start', '>=', kwargs['start_date']))
                    domain.append(('date', '<=', kwargs['end_date']))
            if kwargs.get('project_id'):
                domain.append(('id', '=', int(kwargs['project_id'])))

            current_projects = request.env['project.project'].sudo().search(domain)

            if not current_projects:
                return return_Response(message="Success", status=200, data={"records": {}})

            total_projects = [{
                'stage': p['stage_id'][1] if p['stage_id'] else "Undefined",
                'count': p['stage_id_count']
            } for p in current_projects.sudo().read_group(
                domain=domain,
                fields=['stage_id'],
                groupby=['stage_id']
            )]

            project_count = sum([i['count'] for i in total_projects]) or 0
            project_message = ", ".join([f"{i['count']} in {i['stage']}" for i in total_projects if i['count']]) or ""

            blockers = request.env['project.blocker'].sudo().read_group(
                domain=[('project_id', 'in', current_projects.ids),
                        ('state', 'in', ['raised', 'in_progress', 'testing'])],
                fields=['priority'],
                groupby=['priority']
            )
            blockers_count = sum([i['priority_count'] for i in blockers]) or 0
            blockers_info = ", ".join(
                [f"{block['priority_count']} {priority.get(block['priority'], 'Unknown')}" for block in blockers if block['priority_count']])

            blocker_data = request.env['project.blocker'].sudo().read_group(
                domain=[('state', 'in', ['raised', 'in_progress', 'testing'])],
                fields=['project_id'],
                groupby=['project_id']
            )
            blocker_counts = {item['project_id'][0]: item['project_id_count'] for item in blocker_data if
                              item['project_id']}

            total_task_record = request.env['base.project.task'].sudo().search(
                [('project_id', 'in', current_projects.ids)])
            task_count = len(total_task_record)

            task_data = request.env['base.project.task'].sudo().read_group(
                domain=[('project_id', 'in', current_projects.ids)],
                fields=['status'],
                groupby=['status']
            )

            total_employee = total_task_record.mapped('assignee_ids')
            task_record_dict = {}

            for emp in total_employee:
                employee_task = total_task_record.filtered(lambda t: emp.id in t.assignee_ids.ids)
                approved_task = employee_task.filtered(lambda t: t.status == 'approved')

                task_total = len(employee_task)
                task_record_dict[emp.id] = {
                    'employee_id': emp.id,
                    'employee_name': emp.name,
                    'employee_task_count': task_total,
                    'task_approval_rate': round((len(approved_task) / task_total) * 100, 2) if task_total > 0 else 0
                }
            project_id = current_projects[0] if current_projects else None
            if kwargs.get('project_id'):
                project_id = request.env['project.project'].sudo().search([('id', 'in', int(kwargs['project_id']))], limit=1)
            vals = {
                "project_cart": {
                    'reviewer_count': len(project_id.project_qc_reviewer) if project_id else 0,
                    'tasker_count': len(project_id.project_tasker) if project_id else 0,
                    'kickoff_meeting': project_id.training_event_id.name if project_id and project_id.training_event_id else False,
                    'send_kickoff_mail': bool(project_id.training_event_id) if project_id else False,
                    'is_pl_stage_completed': project_id.is_pl_stage_completed or False,
                    'is_aire_stage_completed': project_id.is_aire_stage_completed or False,
                    'is_swe_stage_completed': project_id.is_swe_stage_completed or False

            },
                "my_project": {
                    'project_count': project_count,
                    'project_message': project_message
                },
                "daily_throughput": {
                    'total_task_count': task_count,
                    'done_task_count': len(total_task_record.filtered(lambda t: t.status == 'done')),
                    'target_percentage': 10.0
                },
                'team_approval_rate': {
                    'target_percentage': 100.0,
                    'done_task_percentage': 100.0,
                    'difference_percentage': 0.0,
                },
                'open_block': {
                    'open_blockers_count': blockers_count,
                    'blockers_info': blockers_info
                },
                'task_pipeline': {
                    'total_projects': project_count,
                    'task_pipeline_graph': [{
                        'stage': i['status'] if i['status'] else "Undefined",
                        'count': i['status_count'],
                        'percentage': round((i['status_count'] / task_count) * 100, 2) if task_count > 0 else 0
                    } for i in task_data]
                },
                'team_performance': list(task_record_dict.values())
            }

            def get_deadline_info(start_date, end_date):
                projects = current_projects.filtered(lambda p: p.date and start_date <= p.date <= end_date)
                results = []
                for pro in projects:
                    diff = (pro.date - today_date).days

                    if diff < 0:
                        due_label = "Overdue"
                    elif diff == 0:
                        due_label = "Today"
                    else:
                        due_label = f"In {diff} day{'s' if diff > 1 else ''}"

                    count = blocker_counts.get(pro.id, 0)
                    message = f"{count} blockers are pending" if count > 0 else f"{pro.stage_id.name or 'Initial'} stage is due"

                    results.append({
                        'name': pro.name,
                        'message': message,
                        'due_in': due_label,
                        'status': 'blocked' if count > 0 else 'clear'
                    })
                return results

            vals["upcoming_deadlines"] = {
                "today": get_deadline_info(today_date, today_date),
                "tomorrow": get_deadline_info(today_date + datetime.timedelta(days=1), today_date + datetime.timedelta(days=1)),
                "this_week": get_deadline_info(today_date + datetime.timedelta(days=2), today_date + datetime.timedelta(days=7))
            }

            return return_Response(message="Success", status=200, data={"records": vals})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_aire_dashboard_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_aire_dashboard_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)

            now = datetime.datetime.now()
            today_date = now.date()
            priority = {
                '0': 'Low',
                '1': 'Medium',
                '2': 'High',
                '3': 'Critical'
            }

            domain = [('project_aire', '=', user_id.employee_id.id)]

            if kwargs.get('start_date') and kwargs.get('end_date'):
                if kwargs['start_date'] == kwargs['end_date']:
                    domain.append(('date_start', '=', kwargs['start_date']))
                else:
                    domain.append(('date_start', '>=', kwargs['start_date']))
                    domain.append(('date', '<=', kwargs['end_date']))
            if kwargs.get('project_id'):
                domain.append(('id', '=', int(kwargs['project_id'])))

            current_projects = request.env['project.project'].sudo().search(domain)

            if not current_projects:
                return return_Response(message="Success", status=200, data={"records": {}})
            project_id = current_projects[0] if current_projects else None
            if kwargs.get('project_id'):
                project_id = request.env['project.project'].sudo().search([('id', 'in', int(kwargs['project_id']))],
                                                                          limit=1)

            total_projects = [{
                'stage': p['stage_id'][1] if p['stage_id'] else "Undefined",
                'count': p['stage_id_count']
            } for p in current_projects.sudo().read_group(
                domain=domain,
                fields=['stage_id'],
                groupby=['stage_id']
            )]

            project_count = sum([i['count'] for i in total_projects]) or 0
            project_message = ", ".join([f"{i['count']} in {i['stage']}" for i in total_projects if i['count']]) or ""

            blockers = request.env['project.blocker'].sudo().read_group(
                domain=[('project_id', 'in', current_projects.ids),
                        ('state', 'in', ['raised', 'in_progress', 'testing'])],
                fields=['priority'],
                groupby=['priority']
            )
            blockers_count = sum([i['priority_count'] for i in blockers]) or 0
            blockers_info = ", ".join(
                [f"{block['priority_count']} {priority.get(block['priority'], 'Unknown')}" for block in blockers if block['priority_count']])


            vals = {
                "project_cart": {
                    'experimentation_design': len(project_id.project_experiment_design) if project_id else 0,
                    'uploaded_document_count': len(project_id.project_tasker) if project_id else 0,
                    'added_skill_tags_count': len(project_id.skill_tags) if project_id else 0,
                    'add_research_note': bool(project_id.research_notes) if project_id else False,
                    'is_pl_stage_completed': project_id.is_pl_stage_completed or False,
                    'is_aire_stage_completed': project_id.is_aire_stage_completed or False,
                    'is_swe_stage_completed': project_id.is_swe_stage_completed or False
                },
                "assigned_project": {
                    'project_count': project_count,
                    'project_message': project_message
                },
                'rfp_portal_pending': {
                    'count': request.env['project.project'].sudo().search_count(domain + [('stage_id', '=', request.env.ref('project_extension.project_project_stage_ethara_4', raise_if_not_found=False).id)]),
                    'message': f"{project_id.name} - your portal incompleted."
                },
                'open_block': {
                    'open_blockers_count': blockers_count,
                    'blockers_info': blockers_info
                },
                'your_projects': [{'id': i.id, 'name': i.name, 'client_name': i.client_name, 'project_seq': i.project_seq, 'status': i.stage_id.name, 'write_date': str(i.write_date),
                    'is_pl_stage_completed': i.is_pl_stage_completed or False,
                    'is_aire_stage_completed': i.is_aire_stage_completed or False,
                    'is_swe_stage_completed': i.is_swe_stage_completed or False,
                    'blocker_count': request.env['project.blocker'].sudo().search_count([('project_id', 'in', current_projects.ids), ('state', 'in', ['raised', 'in_progress', 'testing'])]),
                    'total_task_count': len(i.base_project_task),
                    'done_task_count': len(i.base_project_task.filtered(lambda p: p.status == 'delivery')),
                    'difference_percentage': round(len(i.base_project_task.filtered(lambda p: p.status == 'delivery')) / len(i.base_project_task), 2) if i.base_project_task else 0.0
                    } for i in current_projects]

            }

            return return_Response(message="Success", status=200, data={"records": vals})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_swe_dashboard_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_swe_dashboard_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)

            now = datetime.datetime.now()
            today_date = now.date()
            priority = {
                '0': 'Low',
                '1': 'Medium',
                '2': 'High',
                '3': 'Critical'
            }

            domain = [('project_swe', '=', user_id.employee_id.id)]

            if kwargs.get('start_date') and kwargs.get('end_date'):
                if kwargs['start_date'] == kwargs['end_date']:
                    domain.append(('date_start', '=', kwargs['start_date']))
                else:
                    domain.append(('date_start', '>=', kwargs['start_date']))
                    domain.append(('date', '<=', kwargs['end_date']))
            if kwargs.get('project_id'):
                domain.append(('id', '=', int(kwargs['project_id'])))

            current_projects = request.env['project.project'].sudo().search(domain)

            if not current_projects:
                return return_Response(message="Success", status=200, data={"records": {}})
            project_id = current_projects[0] if current_projects else None
            if kwargs.get('project_id'):
                project_id = request.env['project.project'].sudo().search([('id', 'in', int(kwargs['project_id']))],
                                                                          limit=1)

            total_projects = [{
                'stage': p['stage_id'][1] if p['stage_id'] else "Undefined",
                'count': p['stage_id_count']
            } for p in current_projects.sudo().read_group(
                domain=domain,
                fields=['stage_id'],
                groupby=['stage_id']
            )]

            project_count = sum([i['count'] for i in total_projects]) or 0
            project_message = ", ".join([f"{i['count']} in {i['stage']}" for i in total_projects if i['count']]) or ""

            blockers = request.env['project.blocker'].sudo().read_group(
                domain=[('project_id', 'in', current_projects.ids),
                        ('state', 'in', ['raised', 'in_progress', 'testing'])],
                fields=['priority'],
                groupby=['priority']
            )
            blockers_count = sum([i['priority_count'] for i in blockers]) or 0
            blockers_info = ", ".join(
                [f"{block['priority_count']} {priority.get(block['priority'], 'Unknown')}" for block in blockers if block['priority_count']])


            vals = {
                "project_cart": {
                    'api_configuration': False,
                    'infrastructure_setup': len(project_id.project_infrastructure_requirement) if project_id else 0,
                    'technical_configuration': False,
                    'is_pl_stage_completed': project_id.is_pl_stage_completed or False,
                    'is_aire_stage_completed': project_id.is_aire_stage_completed or False,
                    'is_swe_stage_completed': project_id.is_swe_stage_completed or False
                },
                "assigned_project": {
                    'project_count': project_count,
                    'project_message': project_message
                },
                'rfp_portal_pending': {
                    'count': request.env['project.project'].sudo().search_count(domain + [('stage_id', '=', request.env.ref('project_extension.project_project_stage_ethara_4', raise_if_not_found=False).id)]),
                    'message': f"{project_id.name} - your portal incompleted."
                },
                'open_block': {
                    'open_blockers_count': blockers_count,
                    'blockers_info': blockers_info
                },
                'your_projects': [{'id': i.id, 'name': i.name, 'client_name': i.client_name, 'project_seq': i.project_seq, 'status': i.stage_id.name, 'write_date': str(i.write_date),
                    'is_pl_stage_completed': i.is_pl_stage_completed or False,
                    'is_aire_stage_completed': i.is_aire_stage_completed or False,
                    'is_swe_stage_completed': i.is_swe_stage_completed or False,
                    'blocker_count': request.env['project.blocker'].sudo().search_count([('project_id', 'in', current_projects.ids), ('state', 'in', ['raised', 'in_progress', 'testing'])]),
                    'total_task_count': len(i.base_project_task),
                    'done_task_count': len(i.base_project_task.filtered(lambda p: p.status == 'delivery')),
                    'difference_percentage': round(len(i.base_project_task.filtered(lambda p: p.status == 'delivery')) / len(i.base_project_task), 2) if i.base_project_task else 0.0
                    } for i in current_projects]

            }

            return return_Response(message="Success", status=200, data={"records": vals})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
