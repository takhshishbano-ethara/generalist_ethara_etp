import datetime

from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request, safe_get_value
)

# Backend job models have a different state vocabulary
# than the legacy task.forge.log. "Active / currently working" maps to the
# pipeline-running states below. Done/terminal states live in DONE_STATES so
# the two are kept in one place.
ACTIVE_STATES = ('extracting', 'extracted', 'generating', 'generated', 'scoring', 'scored', 'qc_running')
DONE_STATES = ('done', 'submitted')


class DashboardController(http.Controller):

    @validate_token
    @http.route('/api/v2/get_cto_dashboard_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def v2_get_cto_dashboard_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            is_cto = (
                user_id.has_group('etp_user_roles.group_cto')
                or user_id.user_role.id == request.env.ref('api_auth_gateway.role_cto_technical').id
            )
            is_tpm = not is_cto and (
                user_id.has_group('etp_user_roles.group_tpm')
                or user_id.user_role.id == request.env.ref('api_auth_gateway.role_tpm_technical').id
            )
            if not (is_cto or is_tpm):
                return return_Response(message="CTO or TPM role required", status=403)
            employee = user_id.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Employee = request.env['hr.employee'].sudo()
            if is_cto:
                team_ids = employee._get_team_employee_ids()
                pls = Employee  
                team_user_ids = None 
            else:
                pls = Employee.search([
                    ('task_forge_tpm_id', '=', employee.id),
                    ('task_forge_active', '=', True),
                ])
                team_ids = Employee.search([
                    '|', '|',
                    ('task_forge_tpm_id', '=', employee.id),
                    ('task_forge_pl_id', 'in', pls.ids),
                    ('id', '=', employee.id),
                    ('task_forge_active', '=', True),
                ]).ids
                team_user_ids = Employee.browse(team_ids).mapped('user_id').ids

            priority = {
                '0': 'Low',
                '1': 'Medium',
                '2': 'High',
                '3': 'Critical'
            }
            now = datetime.datetime.now()


            present_today = request.env['hr.attendance'].sudo().search([('check_in', '>=', f"{datetime.datetime.now().date()} 00:00:00"), ('employee_id', 'in', team_ids), ('attendance_status', '=', 'present')])
            present_yesterday_today = request.env['hr.attendance'].sudo().search_count([('check_in', '>=', f"{datetime.datetime.now().date() - datetime.timedelta(days=1)} 00:00:00"), ('check_out', '<=', f"{datetime.datetime.now().date() - datetime.timedelta(days=1)} 23:59:00"), ('employee_id', 'in', team_ids), ('attendance_status', '=', 'present')])
            on_leave_count = request.env['hr.leave'].sudo().search_count([
                ('employee_id', 'in', team_ids),
                ('state', '=', 'validate'),
                ('date_from', '<=', datetime.datetime.now().date()),
                ('date_to', '>=', datetime.datetime.now().date())
            ])
            pending_leave_count = request.env['hr.leave'].sudo().search_count([
                            ('employee_id', 'in', team_ids),
                            ('state', '=', 'confirm'),
                            # ('date_from', '<=', datetime.datetime.now().date()),
                            # ('date_to', '>=', datetime.datetime.now().date())
                        ])

            
            complete_task_count = 0
            daily_counts = {}
            log_start = kwargs.get('start_date')
            log_end = kwargs.get('end_date')
            project_id_raw = kwargs.get('project_id')
            domain = [('connected_table', '!=', False)]
            if is_tpm:
                domain += [('project_lead', 'in', pls.ids)] + request.env['project.project']._task_forge_live_domain()
            if project_id_raw:
                domain.append(('id', '=', int(project_id_raw)))
            projects = request.env['project.project'].sudo().search(domain)
            # Date window for the completion graph, used by all branches.
            if log_start and log_end:
                if log_start == log_end:
                    dt_from = f"{log_start} 00:00:00"
                    dt_to = f"{log_start} 23:59:00"
                else:
                    dt_from = f"{log_start} 00:00:00"
                    dt_to = f"{log_end} 23:59:00"
            else:
                dt_from = dt_to = None

            for project in projects:
                backend = request.env[project.connected_table].sudo()
                # Total task count — dispatch by role.
                if is_cto:
                    if hasattr(backend, 'get_performance_metrics'):
                        project_metrics = backend.get_performance_metrics() or {}
                        complete_task_count += project_metrics.get('total_task_count', 0)
                else:
                    if not team_user_ids:
                        continue
                    if hasattr(backend, 'count_user_tasks'):
                        complete_task_count += backend.count_user_tasks(team_user_ids)

                # Per-day done counts for the completion graph.
                if hasattr(backend, 'get_completed_daily_counts'):
                    user_filter = team_user_ids if is_tpm else None
                    for day, count in backend.get_completed_daily_counts(
                        user_ids=user_filter, dt_from=dt_from, dt_to=dt_to,
                    ):
                        if day:
                            daily_counts[day] = daily_counts.get(day, 0) + count
            
            # blockers = request.env['task.forge.blocker'].sudo().read_group(
            #     domain=[('state', 'not in', ['no_issue', 'resolved'])],
            #     fields=['priority'],
            #     groupby=['priority']
            # )
            #blockers_count = request.env['task.forge.blocker'].sudo().search_count(domain=[('state', 'not in', ['no_issue', 'resolved'])])
            #blockers_info = ", ".join([f"{block['priority_count']} {priority[block['priority']]}" for block in blockers])
            diff_percent = 0.0

            if present_yesterday_today > 0:
                diff_percent = ((len(present_today) - present_yesterday_today) / present_yesterday_today) * 100
            elif len(present_today) > 0:
                diff_percent = 100.0
            labels = list(daily_counts.keys())
            values = list(daily_counts.values())
            pl_record = present_today.filtered(lambda a: a.employee_id.user_id.user_role.id in [request.env.ref('api_auth_gateway.role_pl_technical').id, request.env.ref('api_auth_gateway.role_pl_stem').id, request.env.ref('api_auth_gateway.role_pl_non_stem').id])
            qc_record = present_today.filtered(lambda a: a.employee_id.user_id.user_role.id in [request.env.ref('api_auth_gateway.role_qc_technical').id, request.env.ref('api_auth_gateway.role_qc_stem').id, request.env.ref('api_auth_gateway.role_qc_non_stem').id])
            tasker_record = present_today.filtered(lambda a: a.employee_id.user_id.user_role.id in [request.env.ref('api_auth_gateway.role_tasker_technical').id, request.env.ref('api_auth_gateway.role_tasker_stem').id, request.env.ref('api_auth_gateway.role_tasker_non_stem').id])

            vals = {
                'total_member':{
                    'total_member_count': len(set(team_ids)),
                    'total_pl_count': request.env['hr.employee'].sudo().search_count([('id', 'in', team_ids), ('user_id.user_role', 'in', [request.env.ref('api_auth_gateway.role_pl_technical').id, request.env.ref('api_auth_gateway.role_pl_stem').id, request.env.ref('api_auth_gateway.role_pl_non_stem').id])]),
                    'total_qc_count': request.env['hr.employee'].sudo().search_count([('id', 'in', team_ids), ('user_id.user_role', 'in', [request.env.ref('api_auth_gateway.role_qc_technical').id, request.env.ref('api_auth_gateway.role_qc_stem').id, request.env.ref('api_auth_gateway.role_qc_non_stem').id])]),
                    'total_tasker_count': request.env['hr.employee'].sudo().search_count([('id', 'in', team_ids), ('user_id.user_role', 'in', [request.env.ref('api_auth_gateway.role_tasker_technical').id, request.env.ref('api_auth_gateway.role_tasker_stem').id, request.env.ref('api_auth_gateway.role_tasker_non_stem').id])]),
                    'total_present_pl_count': len(present_today.filtered(lambda x: x.employee_id in pl_record.mapped('employee_id'))),
                    'total_present_qc_count': len(present_today.filtered(lambda x: x.employee_id in qc_record.mapped('employee_id'))),
                    'total_present_tasker_count': len(present_today.filtered(lambda x: x.employee_id in tasker_record.mapped('employee_id')))
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
                    'open_blockers_count': 0,
                    'blocker_info': ""
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
            if not (user_id.has_group('etp_user_roles.group_project_lead') or user_id.user_role.id in [
                request.env.ref('api_auth_gateway.role_pl_technical').id,
                request.env.ref('api_auth_gateway.role_pl_stem').id,
                request.env.ref('api_auth_gateway.role_pl_non_stem').id,
            ]):
                return return_Response(message="PL role required", status=403)
            employee = user_id.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()

            pending_leave_count = request.env['hr.leave'].sudo().search_count([
                ('employee_id', 'in', team_ids),
                ('state', '=', 'confirm'),
            ])
            escalated_task_count = request.env['task.forge.blocker'].sudo().search_count([
                ('state', 'in', ['escalated_to_pl']),
                ('employee_id', 'in', team_ids),
            ])

            daily_counts = {}
            log_start = kwargs.get('start_date')
            log_end = kwargs.get('end_date')
            project_id_raw = kwargs.get('project_id')
            project_domain = [('connected_table', '!=', False), ('project_lead', '=', employee.id)] + request.env['project.project']._task_forge_live_domain()
            if project_id_raw:
                project_domain.append(('id', '=', int(project_id_raw)))
            current_projects = request.env['project.project'].sudo().search(project_domain)
            if log_start and log_end:
                if log_start == log_end:
                    dt_from = f"{log_start} 00:00:00"
                    dt_to = f"{log_start} 23:59:00"
                else:
                    dt_from = f"{log_start} 00:00:00"
                    dt_to = f"{log_end} 23:59:00"
            else:
                dt_from = dt_to = None
            seen_backends = set()
            for project in current_projects:
                backend_name = project.connected_table
                if not backend_name or backend_name in seen_backends:
                    continue
                seen_backends.add(backend_name)
                if backend_name not in request.env:
                    continue
                backend = request.env[backend_name].sudo()
                if not hasattr(backend, 'get_completed_daily_counts'):
                    continue
                for day, count in backend.get_completed_daily_counts(
                    dt_from=dt_from, dt_to=dt_to,
                ):
                    if day:
                        daily_counts[day] = daily_counts.get(day, 0) + count

            labels = list(daily_counts.keys())
            values = list(daily_counts.values())

            vals = {
                'total_task': {
                    'total_task_count': len(team_ids),
                    'active_projects': len(current_projects),
                },
                'active_project': {
                    'active_project_count': len(current_projects),
                },
                'escalated_task': {
                    'escalated_task_count': escalated_task_count,
                },
                'pending_leave_count': {
                    'pending_leave_count': pending_leave_count,
                },
                'task_completion_graph': {
                    'days': labels,
                    'total_completed': values,
                },
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
            team_ids = user_id.employee_id._get_team_employee_ids()

            # domain = [('project_lead', '=', user_id.employee_id.id), ('non_stemp_project_status', 'in', ['production'])]
            # if kwargs.get('project_type'):
            #     domain.append(('y_project_type', '=', kwargs.get('project_type')))
            #
            # if kwargs.get('start_date') and kwargs.get('end_date'):
            #     if kwargs['start_date'] == kwargs['end_date']:
            #         domain.append(('date_start', '=', kwargs['start_date']))
            #     else:
            #         domain.append(('date_start', '>=', kwargs['start_date']))
            #         domain.append(('date', '<=', kwargs['end_date']))
            #
            # current_projects = request.env['project.project'].sudo().search(domain)
            # for project in current_projects:
            employees = request.env['hr.employee'].sudo().search([('id', 'in', team_ids)])

            # All distinct backend models registered across the org's projects.
            # Computed once and reused per-employee so the active check works
            # for taskers/QRs/QLs who aren't leads of any project.
            all_backends = set(
                request.env['project.project'].sudo()
                .search([('connected_table', '!=', False)])
                .mapped('connected_table')
            )

            for emp in employees:
                project = request.env['project.project'].sudo().search([('project_lead', '=', emp.id)] + request.env['project.project']._task_forge_live_domain(), limit=1)

                # Active = the employee has any in-progress row on any backend.
                # Backends decide which states count as "active" inside
                # their get_active_users implementation.
                is_active = False
                if emp.user_id:
                    for backend_name in all_backends:
                        if backend_name not in request.env:
                            continue
                        backend = request.env[backend_name].sudo()
                        if not hasattr(backend, 'get_active_users'):
                            continue
                        if backend.get_active_users(user_ids=[emp.user_id.id]):
                            is_active = True
                            break

                temp.append({
                    'name':emp.name if emp.name else "",
                    'project_name':project.name if project and project.name else "",
                    'qr_name':emp.task_forge_qr_id.name if emp.task_forge_qr_id.name else "",
                    'status': 'Active' if is_active else "Idle"
                })
            return return_Response(message="Success", status=200, data={"records": temp, "count": len(temp)})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_qc_dashboard_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_qc_dashboard_list(self, **kwargs):
        try:
            user = request.env.user
            if not (user.has_group('etp_user_roles.group_quality_lead') or user.has_group('etp_user_roles.group_quality_reviewer') or user.user_role.id in [
                request.env.ref('api_auth_gateway.role_qc_technical').id,
                request.env.ref('api_auth_gateway.role_qc_stem').id,
                request.env.ref('api_auth_gateway.role_qc_non_stem').id,
            ]):
                return return_Response(message="Quality role required", status=403)
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            total_tasker = request.env['hr.employee'].sudo().search([('task_forge_qr_id', '=', employee.id)])
            tasker_user_ids = total_tasker.mapped('user_id').ids

            log_start = kwargs.get('start_date')
            log_end = kwargs.get('end_date')
            project_id_raw = kwargs.get('project_id')
            project_domain = [('connected_table', '!=', False), ('project_qc_reviewer', '=', employee.id)] + request.env['project.project']._task_forge_live_domain()
            if project_id_raw:
                project_domain.append(('id', '=', int(project_id_raw)))
            current_projects = request.env['project.project'].sudo().search(project_domain)

            today_start = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
            today_end = datetime.datetime.combine(datetime.date.today(), datetime.time.max)

            working_user_ids = set()
            task_done_today = 0
            daily_counts = {}
            if log_start and log_end:
                if log_start == log_end:
                    dt_from = f"{log_start} 00:00:00"
                    dt_to = f"{log_start} 23:59:00"
                else:
                    dt_from = f"{log_start} 00:00:00"
                    dt_to = f"{log_end} 23:59:00"
            else:
                dt_from = dt_to = None
            seen_backends = set()
            for project in current_projects:
                backend_name = project.connected_table
                if not backend_name or backend_name in seen_backends:
                    continue
                seen_backends.add(backend_name)
                if backend_name not in request.env:
                    continue
                backend = request.env[backend_name].sudo()

                # Which of this QC's taskers have any in-progress work?
                if tasker_user_ids and hasattr(backend, 'get_active_users'):
                    working_user_ids.update(
                        backend.get_active_users(user_ids=tasker_user_ids)
                    )

                # Task done today across this backend (project-wide).
                if hasattr(backend, 'get_completed_daily_counts'):
                    for _day, cnt in backend.get_completed_daily_counts(
                        dt_from=today_start, dt_to=today_end,
                    ):
                        task_done_today += cnt

                # Review-throughput daily graph over the requested window.
                if hasattr(backend, 'get_completed_daily_counts'):
                    for day, count in backend.get_completed_daily_counts(
                        dt_from=dt_from, dt_to=dt_to,
                    ):
                        if day:
                            daily_counts[day] = daily_counts.get(day, 0) + count

            working_taskers = total_tasker.filtered(lambda t: t.user_id and t.user_id.id in working_user_ids)
            idle_taskers = total_tasker - working_taskers

            pending_blocker_count = request.env['task.forge.blocker'].sudo().search_count([
                ('qr_id', '=', employee.id),
                ('state', 'not in', ['no_issue', 'resolved']),
            ])

            pending_leave_count = request.env['hr.leave'].sudo().search_count([
                ('employee_id', 'in', total_tasker.ids),
                ('state', '=', 'confirm'),
            ])

            labels = list(daily_counts.keys())
            values = list(daily_counts.values())

            vals = {
                'my_tasker': {
                    'total_count': len(total_tasker),
                    'working_count': len(working_taskers),
                    'idle_count': len(idle_taskers),
                },
                'task_today': {
                    'task_done_today': task_done_today,
                },
                'pending_blocker': {
                    'count': pending_blocker_count,
                },
                'pending_leave': {
                    'count': pending_leave_count,
                },
                'review_throughput_graph': {
                    'days': labels,
                    'total_completed': values,
                },
                'reporting_to': employee.task_forge_pl_id.name if employee.task_forge_pl_id and employee.task_forge_pl_id.name else "",
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

            domain = [('project_qc_reviewer', '=', user_id.employee_id.id)] + request.env['project.project']._task_forge_live_domain()
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
                # per-tasker aggregation: dispatch to each backend.
                # Backends hide their own user / state / duration / date
                # field names inside `get_user_task_breakdown`.
                total_count = 0
                done_count = 0
                today_count = 0
                total_seconds = 0
                today_start = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
                today_end = datetime.datetime.combine(datetime.date.today(), datetime.time.max)
                seen_backends_for_tasker = set()
                tasker_user_id = tasker.user_id.id if tasker.user_id else 0
                for project in current_projects:
                    backend_name = project.connected_table
                    if not backend_name or backend_name in seen_backends_for_tasker:
                        continue
                    seen_backends_for_tasker.add(backend_name)
                    if backend_name not in request.env or not tasker_user_id:
                        continue
                    backend = request.env[backend_name].sudo()
                    if not hasattr(backend, 'get_user_task_breakdown'):
                        continue
                    m = backend.get_user_task_breakdown(
                        tasker_user_id, today_start, today_end,
                    ) or {}
                    total_count += m.get('total', 0) or 0
                    done_count += m.get('done', 0) or 0
                    today_count += m.get('today', 0) or 0
                    total_seconds += m.get('seconds', 0) or 0
                avg_mins = (total_seconds / 60.0 / total_count) if total_count > 0 else 0.0
                hours, mins = divmod(int(round(avg_mins)), 60)
                duration_display = f"{hours:02d}:{mins:02d}"
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
                    'today_task': today_count,
                    'quality': round(completion_percent, 2),
                    'completion': round(completion_percent, 2),
                    'aht': duration_display,
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

                # query the project's backend model directly via
                # `connected_table`, instead of going through task.forge.log.
                # Assumes one backend = one project's data (the convention the
                # `connected_table` field encodes today).
                # State vocabulary: backend models use 'done'/'submitted' for
                # what task.forge.log called 'completed'.
                backend_name = p.connected_table
                total = done = 0
                if backend_name and backend_name in request.env:
                    backend = request.env[backend_name].sudo()
                    if hasattr(backend, 'get_project_totals'):
                        m = backend.get_project_totals() or {}
                        total = m.get('total', 0) or 0
                        done = m.get('done', 0) or 0

                percentage = (done / total * 100) if total > 0 else 0.0

                project_data.append({
                    'id': safe_get_value(p, 'id', 'int'),
                    'project_name': safe_get_value(p, 'name', 'str'),
                    'project_id_code': safe_get_value(p, 'project_seq', 'str'),
                    'client': safe_get_value(p, 'client_name', 'str'),
                    'status': safe_get_value(p, 'non_stemp_project_status', 'str') if p.project_category == 'non_stem' else safe_get_value(p, 'stage_id.name', 'str'),
                    'progress': percentage,
                    'tasks': safe_get_value(p, 'sample_task_number', 'int'),
                    'team_count': unique_team_count,
                    'blockers': request.env['task.forge.blocker'].sudo().search_count([('state', 'not in', ['no_issue', 'resolved']), ('project_id', '=', p.id)]),
                    'category': safe_get_value(p, 'project_category', 'str'),
                    'type': safe_get_value(p, 'project_type', 'str'),
                    'date_start': safe_get_value(p, 'date_start', 'date'),
                    'date_end': safe_get_value(p, 'date', 'date'),
                })
            return return_Response(
                message="Success",
                status=200,
                data={"record": project_data, "total_record_count": len(project_data), "count": len(project_data), "default-project": project_data[0] if project_data else {}})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_employee_project_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_employee_project_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)

            employee = user_id.employee_id
            domain = request.env['project.project']._task_forge_live_domain()
            task_domain = []

            if kwargs.get('show_all') in [1, '1']:
                domain = []

            if user_id.user_role.id == request.env.ref('api_auth_gateway.role_cto_technical').id:
                domain = []
                task_domain = []

            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_pl_technical').id, request.env.ref('api_auth_gateway.role_pl_stem').id, request.env.ref('api_auth_gateway.role_pl_non_stem').id]:
                domain.append(('project_lead', '=', employee.id))
                task_domain.append(('employee_id.task_forge_qr_id.task_forge_pl_id', '=', employee.id))

            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_qc_technical').id, request.env.ref('api_auth_gateway.role_qc_stem').id, request.env.ref('api_auth_gateway.role_qc_non_stem').id]:
                domain.append(('project_qc_reviewer', '=', employee.id))
                task_domain.append(('employee_id.task_forge_qr_id', '=', employee.id))

            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_tasker_technical').id, request.env.ref('api_auth_gateway.role_tasker_stem').id, request.env.ref('api_auth_gateway.role_tasker_non_stem').id]:
                domain.append(('project_tasker', '=', employee.id))
                task_domain.append(('employee_id', '=', employee.id))

            search = kwargs.get('search')
            if search:
                domain += ['|', ('name', 'ilike', search), ('internal_project_name', 'ilike', search)]

            if kwargs.get('status'):
                if 'all' not in kwargs.get('status'):
                    status_list = [int(x.strip()) for x in kwargs.get('status').split(',') if x.strip()]
                    domain += [('stage_id', 'in', status_list)]

            page = int(kwargs.get('page')) if kwargs.get('page') else 1
            limit = int(kwargs.get('limit')) if kwargs.get('limit') else 10
            offset = (page - 1) * limit
            total_count = request.env['project.project'].sudo().search_count(domain)
            if not kwargs.get('page'):
                limit = total_count
                offset = 0
            projects = request.env['project.project'].sudo().search(domain, order='id desc', limit=limit, offset=offset)
            project_data = []
            # TaskLog = request.env['task.forge.log'].sudo()  # replaced by per-project backend lookup
            for p in projects:
                all_member_ids = set(
                    p.project_lead.ids + p.project_qc_reviewer.ids +
                    p.project_tasker.ids + p.project_aire.ids + p.project_swe.ids
                )

                # per-project counts + AHT from the project's backend.
                # Assumes one backend = one project (the convention encoded in
                # `connected_table`). AHT loses the role-based `task_domain`
                # scoping since backend rows have no employee_id; it now reflects
                # all jobs on this backend.
                backend_name = p.connected_table
                total = done = aht_time = 0
                if backend_name and backend_name in request.env:
                    backend = request.env[backend_name].sudo()
                    if hasattr(backend, 'get_project_totals'):
                        m = backend.get_project_totals() or {}
                        total = m.get('total', 0) or 0
                        done = m.get('done', 0) or 0
                        aht_time = m.get('aht_minutes', 0) or 0
                percentage = (done / total * 100) if total > 0 else 0.0

                pl_names = ', '.join(p.project_lead.mapped('name')) if p.project_lead else ''
                qr_names = ', '.join(p.project_qc_reviewer.mapped('name')) if p.project_qc_reviewer else ''

                project_data.append({
                    'id': safe_get_value(p, 'id', 'int'),
                    'project_name': safe_get_value(p, 'name', 'str'),
                    'project_id_code': safe_get_value(p, 'project_seq', 'str'),
                    'client': safe_get_value(p, 'client_name', 'str'),
                    'status': safe_get_value(p, 'non_stemp_project_status', 'str') if p.project_category == 'non_stem' else safe_get_value(p, 'stage_id.name', 'str'),
                    'progress': percentage,
                    'tasks': total,
                    'team_count': len(all_member_ids),
                    'pl_name': pl_names,
                    'qr_name': qr_names,
                    'blockers': request.env['task.forge.blocker'].sudo().search_count([('state', 'not in', ['no_issue', 'resolved']), ('project_id', '=', p.id)]),
                    'category': safe_get_value(p, 'project_category', 'str'),
                    'type': safe_get_value(p, 'project_type', 'str'),
                    'date_start': safe_get_value(p, 'date_start', 'date'),
                    'date_end': safe_get_value(p, 'date', 'date'),
                    'aht_time': aht_time,
                    'tab_list': [{'tab_name': tab.table_name or "", 'api_end_point': tab.api_prefix or ""} for tab in p.api_map_ids],
                    'project_classification': safe_get_value(p, 'project_classification', 'str'),
                })
            return return_Response(
                message="Success",
                status=200,
                data={"record": project_data, "total_record_count": total_count, "count": len(project_data)})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_active_project_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_active_project_list(self, **kwargs):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)
            domain = request.env['project.project']._task_forge_live_domain()

            if user_id.user_role.id in [request.env.ref('api_auth_gateway.role_pl_technical').id, request.env.ref('api_auth_gateway.role_pl_stem').id, request.env.ref('api_auth_gateway.role_pl_non_stem').id]:
                domain += [('project_lead', '=', user_id.employee_id.id)]

            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_qc_technical').id, request.env.ref('api_auth_gateway.role_qc_stem').id, request.env.ref('api_auth_gateway.role_qc_non_stem').id]:
                domain += [('project_qc_reviewer', '=', user_id.employee_id.id)]

            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_tasker_technical').id, request.env.ref('api_auth_gateway.role_tasker_stem').id, request.env.ref('api_auth_gateway.role_tasker_non_stem').id]:
                domain += [('project_tasker', '=', user_id.employee_id.id)]
            projects = request.env['project.project'].sudo().search(domain, order='create_date desc')
            project_data = []
            for p in projects:
                project_data.append({
                    'id': safe_get_value(p, 'id', 'int'),
                    'project_name': safe_get_value(p, 'name', 'str'),
                    'project_id_code': safe_get_value(p, 'project_seq', 'str'),
                    'client': safe_get_value(p, 'client_name', 'str'),
                    'status': safe_get_value(p, 'non_stemp_project_status','str')
                })
            return return_Response(
                message="Success",
                status=200,
                data={"record": project_data, "total_record_count": len(project_data), "count": len(project_data)})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_tasker_dashboard_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_tasker_dashboard_list(self, **kwargs):
        try:
            user = request.env.user
            if not (user.has_group('etp_user_roles.group_tasker') or user.user_role.id in [
                request.env.ref('api_auth_gateway.role_tasker_technical').id,
                request.env.ref('api_auth_gateway.role_tasker_stem').id,
                request.env.ref('api_auth_gateway.role_tasker_non_stem').id,
            ]):
                return return_Response(message="Tasker role required", status=403)
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            today = datetime.date.today()
            today_start = datetime.datetime.combine(today, datetime.time.min)
            today_end = datetime.datetime.combine(today, datetime.time.max)

            attendance = request.env['hr.attendance'].sudo().search([
                ('employee_id', '=', employee.id),
                ('check_in', '>=', today_start),
                ('check_in', '<=', today_end),
            ], limit=1)
            duration_display = "00.00"
            if attendance and attendance.check_in:
                end = attendance.check_out or datetime.datetime.now()
                total_seconds = int((end - attendance.check_in).total_seconds())
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                duration_display = f"{hours:02d}.{minutes:02d}"

            log_start = kwargs.get('start_date')
            log_end = kwargs.get('end_date')
            project_id_raw = kwargs.get('project_id')
            project_domain = [('connected_table', '!=', False), ('project_tasker', '=', employee.id)] + request.env['project.project']._task_forge_live_domain()
            if project_id_raw:
                project_domain.append(('id', '=', int(project_id_raw)))
            current_projects = request.env['project.project'].sudo().search(project_domain)

            user_id_val = user.id
            completed_today_count = 0
            productive_seconds = 0
            daily_quality = {}
            seen_backends = set()
            for project in current_projects:
                backend_name = project.connected_table
                if not backend_name or backend_name in seen_backends:
                    continue
                seen_backends.add(backend_name)
                if backend_name not in request.env:
                    continue
                backend = request.env[backend_name].sudo()

                # Today's completed + productive seconds — dispatched.
                # Backends hide their own worker / state / duration field
                # names inside `get_user_today_summary`.
                if hasattr(backend, 'get_user_today_summary'):
                    s = backend.get_user_today_summary(
                        user_id_val, today_start, today_end,
                    ) or {}
                    completed_today_count += s.get('completed', 0) or 0
                    productive_seconds += s.get('seconds', 0) or 0

                # Per-day quality trend — dispatch to the backend so each
                # one hides its own quality field name (`score`,
                # `quality_score`, …). Backends without the method are
                # silently skipped.
                if hasattr(backend, 'get_quality_trend'):
                    if log_start and log_end:
                        if log_start == log_end:
                            dt_from = f"{log_start} 00:00:00"
                            dt_to = f"{log_start} 23:59:00"
                        else:
                            dt_from = f"{log_start} 00:00:00"
                            dt_to = f"{log_end} 23:59:00"
                    else:
                        dt_from = dt_to = None
                    for day, score in backend.get_quality_trend(user_id_val, dt_from=dt_from, dt_to=dt_to):
                        if day:
                            daily_quality[day] = daily_quality.get(day, 0) + score

            hours, rem = divmod(int(productive_seconds), 3600)
            minutes = rem // 60
            productive_duration = f"{hours:02d}.{minutes:02d}"

            pending_blocker_count = request.env['task.forge.blocker'].sudo().search_count([
                ('employee_id', '=', employee.id),
                ('state', '=', 'pending'),
            ])

            labels = list(daily_quality.keys())
            quality_sums = list(daily_quality.values())

            vals = {
                'session_duration': duration_display,
                'completed_task_count': completed_today_count,
                'productive_duration': productive_duration,
                'pending_blocker_count': pending_blocker_count,
                'quality_trend_graph': {
                    'days': labels,
                    'total_completed': quality_sums,
                },
                'daily_streak': today.weekday() + 1,
            }
            return return_Response(message="Success", status=200, data=vals)

        except Exception as e:
            return return_Response(message="Dashboard Load Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v2/get_tasker_name_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_tasker_name_list(self, **kwargs):
        temp = []
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)
            total_tasker = request.env['hr.employee'].sudo().search([('task_forge_qr_id', '=', user_id.employee_id.id)])
            for tasker in total_tasker:
                temp.append({
                    'id': tasker.id if tasker else 0,
                    'name': tasker.name if tasker.name else ""                })
            return return_Response(message="Success", status=200, data={"records": temp, "count": len(temp)})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])


