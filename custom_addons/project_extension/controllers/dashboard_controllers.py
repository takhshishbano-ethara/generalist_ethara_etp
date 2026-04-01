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
            # if not user_id.employee_id:
            #     return return_Response(message="Employee not found", status=404)
            priority = {
                '0': 'Low',
                '1': 'Medium',
                '2': 'High',
                '3': 'Critical'
            }
            now = datetime.datetime.now()
            last_month = now - datetime.timedelta(days=30)

            # domain = ['|', '|', '|', '|',
            #           ('project_lead', 'in', [user_id.employee_id.id]),
            #           ('project_aire', 'in', [user_id.employee_id.id]),
            #           ('project_swe', 'in', [user_id.employee_id.id]),
            #           ('project_qc_reviewer', 'in', [user_id.employee_id.id]),
            #           ('project_tasker', 'in', [user_id.employee_id.id])
            #           ]
            domain = []
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
                    # --- Time Calculation Logic ---
                    remaining_time = pro.date - now.date()
                    total_seconds = remaining_time.total_seconds()

                    if total_seconds < 0:
                        due_label = "Overdue"
                    elif total_seconds >= 86400:  # More than 24 hours
                        days = int(total_seconds // 86400)
                        due_label = f"In {days} day{'s' if days > 1 else ''}"
                    else:
                        hours = int(total_seconds // 3600)
                        due_label = f"In {hours} hour{'s' if hours > 1 else ''}"
                    # ------------------------------

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

            # 2. Populate Vals
            vals["upcoming_deadlines"] = {
                "today": get_deadline_info(today_date, today_date),
                "tomorrow": get_deadline_info(today_date + datetime.timedelta(days=1), today_date + datetime.timedelta(days=1)),
                "this_week": get_deadline_info(today_date + datetime.timedelta(days=2), today_date + datetime.timedelta(days=7))
            }
            # today = now.date()
            # tomorrow = now + datetime.timedelta(days=1)
            # this_week = now + datetime.timedelta(days=7)
            # vals["upcoming_deadlines"] = [{
            #     "Today": [{'name': pro.name, "message": f"{request.env['project.blocker'].sudo().search_count(domain=[('project_id', '=' , pro.id), ('state', 'in', ['raised', 'in_progress', 'testing'])])} blockers are still pending" for pro in request.env['project.project'].sudo().search([('date', '>=', f'{today} 00:00:00'), ('date', '<=', f'{today} 23:59:00')]) if request.env['project.blocker'].sudo().search_count(domain=[('project_id', '=' , pro.id), ('state', 'in', ['raised', 'in_progress', 'testing'])]) else f"Project in {pro.stage_id.name} Stage"],
            #     "Tomorrow": [{} for pro in request.env['project.project'].sudo().search([('date', '>=', f'{tomorrow} 00:00:00'), ('date', '<=', f'{tomorrow} 23:59:00')])],
            #     "This_Week": [{} for pro in request.env['project.project'].sudo().search([('date', '>', f'{tomorrow} 00:00:00'), ('date', '<=', f'{this_week} 23:59:00')])]
            # }]
            # for pro in request.env['project.project'].sudo().search([('date', '>=', f'{today} 00:00:00'), ('date', '<=', f'{today} 23:59:00')]):
            #     blockers = request.env['project.blocker'].sudo().read_grou
            #         domain=[(), ('state', 'in', ['raised', 'in_progress', 'testing'])],
            #         fields=['priority'],
            #         groupby=['priority']
            #     )


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