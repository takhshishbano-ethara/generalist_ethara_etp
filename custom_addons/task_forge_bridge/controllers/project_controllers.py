from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
import datetime
import json


CTO_ROLE_REFS = ['api_auth_gateway.role_cto_technical']
TPM_ROLE_REFS = ['api_auth_gateway.role_tpm_technical']
PL_ROLE_REFS = [
    'api_auth_gateway.role_pl_technical',
    'api_auth_gateway.role_pl_stem',
    'api_auth_gateway.role_pl_non_stem',
]
QR_ROLE_REFS = [
    'api_auth_gateway.role_qc_technical',
    'api_auth_gateway.role_qc_stem',
    'api_auth_gateway.role_qc_non_stem',
]


class TaskForgeProjectController(http.Controller):

    @http.route('/api/v2/taskforge/projects', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_projects(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Project = request.env['project.project'].sudo()
            role = employee._get_task_forge_role()

            if role == 'tasker':
                alloc_ids = request.env['task.forge.allocation'].sudo().search([
                    ('employee_id', '=', employee.id)
                ]).mapped('project_id').ids
                projects = Project.search([
                    '|',
                    ('id', 'in', alloc_ids),
                ] + Project._task_forge_live_domain())
            else:
                projects = Project.search([])

            data = [self._format_project(p) for p in projects]
            return return_Response(message="Projects list", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/projects', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'name': {'type': 'string', 'required': True},
        'category': {'type': 'string', 'required': False},
        'status': {'type': 'string', 'required': False},
        'platform': {'type': 'string', 'required': False},
    })
    def create_project(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="Insufficient permissions", status=403)

            Project = request.env['project.project'].sudo()
            vals = {'name': jdata['name']}
            if jdata.get('status'):
                vals['task_forge_status'] = jdata['status'].lower()
            if jdata.get('platform'):
                vals['task_forge_platform'] = jdata['platform']

            project = Project.create(vals)

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'New Project Created',
                    'message': f'Project "{project.name}" has been created.',
                    'user_id': request.env.user.id,
                    'priority': '1',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass

            return return_Response(
                message="Project created",
                status=200,
                data={'data': self._format_project(project)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/projects/update', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'project_id': {'type': 'int', 'required': True},
        'name': {'type': 'string', 'required': False},
        'status': {'type': 'string', 'required': False},
        'platform': {'type': 'string', 'required': False},
    })
    def update_project(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="Insufficient permissions", status=403)

            Project = request.env['project.project'].sudo()
            project = Project.browse(jdata['project_id'])
            if not project.exists():
                return return_Response(message="Project not found", status=404)

            vals = {}
            if jdata.get('name'):
                vals['name'] = jdata['name']
            if jdata.get('status'):
                vals['task_forge_status'] = jdata['status'].lower()
            if jdata.get('platform'):
                vals['task_forge_platform'] = jdata['platform']

            if vals:
                project.write(vals)

            try:
                changed = ', '.join(vals.keys()) if vals else 'no fields'
                request.env['kubera.notification'].sudo().create({
                    'title': 'Project Updated',
                    'message': f'Project "{project.name}" has been updated ({changed}).',
                    'user_id': request.env.user.id,
                    'priority': '1',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass

            return return_Response(
                message="Project updated",
                status=200,
                data={'data': self._format_project(project)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/allocations', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_allocations(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()
            Allocation = request.env['task.forge.allocation'].sudo()
            allocations = Allocation.search([('employee_id', 'in', team_ids)])

            data = [self._format_allocation(a) for a in allocations]
            return return_Response(message="Allocations list", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/allocations', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'project_id': {'type': 'int', 'required': True},
        'employee_id': {'type': 'int', 'required': True},
    })
    def create_allocation(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="Insufficient permissions", status=403)

            employee = user.employee_id
            Allocation = request.env['task.forge.allocation'].sudo()

            alloc = Allocation.create({
                'project_id': jdata['project_id'],
                'employee_id': jdata['employee_id'],
                'allocated_by_id': employee.id if employee else False,
            })

            return return_Response(
                message="Allocation created",
                status=200,
                data={'data': self._format_allocation(alloc)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/allocations/delete', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'allocation_id': {'type': 'int', 'required': True},
    })
    def delete_allocation(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="Insufficient permissions", status=403)

            Allocation = request.env['task.forge.allocation'].sudo()
            alloc = Allocation.browse(jdata['allocation_id'])
            if not alloc.exists():
                return return_Response(message="Allocation not found", status=404)

            alloc.unlink()
            return return_Response(message="Allocation removed", status=200)
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/allocations/bulk', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'project_id': {'type': 'int', 'required': True},
        'employee_ids': {'type': 'list', 'required': True},
    })
    def bulk_allocate(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="Insufficient permissions", status=403)

            employee = user.employee_id
            Allocation = request.env['task.forge.allocation'].sudo()
            created = []

            for emp_id in jdata['employee_ids']:
                alloc = Allocation.create({
                    'project_id': jdata['project_id'],
                    'employee_id': int(emp_id),
                    'allocated_by_id': employee.id if employee else False,
                })
                created.append(self._format_allocation(alloc))

            return return_Response(
                message=f"{len(created)} allocations created",
                status=200,
                data={'data': created}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/project/dashboard', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'project_id': {'type': 'str', 'required': True},
    })
    def get_project_dashboard(self, **kwargs):
        try:
            project_id = int(kwargs.get('project_id'))
            project = request.env['project.project'].sudo().browse(project_id)
            if not project.exists():
                return return_Response(message="Project not found", status=404)

            today = datetime.datetime.now().date()
            start_of_week = today - datetime.timedelta(days=today.weekday())

            TaskLog = request.env['task.forge.log'].sudo()
            Blocker = request.env['task.forge.blocker'].sudo()
            Employee = request.env['hr.employee'].sudo()

            done_task_count = TaskLog.search_count([
                ('project_id', '=', project_id),
                ('state', '=', 'completed')
            ])

            this_week_new_task_count = TaskLog.search_count([
                ('project_id', '=', project_id),
                ('create_date', '>=', start_of_week)
            ])

            total_task_count = TaskLog.search_count([
                ('project_id', '=', project_id)
            ])
            completion_rate = 0.0
            if total_task_count > 0:
                completion_rate = round((done_task_count / total_task_count) * 100, 2)

            blocker_domain = [('project_id', '=', project_id), ('state', 'not in', ['no_issue', 'resolved'])]
            open_blocker_count = Blocker.search_count(blocker_domain)
            blocker_grouped = Blocker.read_group(
                domain=blocker_domain,
                fields=['state'],
                groupby=['state']
            )
            blocker_by_state = [f"{item['state_count']}, {item['state']}"
                for item in blocker_grouped if item['state_count']
            ]
            blocker_message = ", ".join(blocker_by_state)

            all_roles = [
                request.env.ref('api_auth_gateway.role_pl_technical').id,
                request.env.ref('api_auth_gateway.role_pl_stem').id,
                request.env.ref('api_auth_gateway.role_pl_non_stem').id,
                request.env.ref('api_auth_gateway.role_qc_technical').id,
                request.env.ref('api_auth_gateway.role_qc_stem').id,
                request.env.ref('api_auth_gateway.role_qc_non_stem').id,
                request.env.ref('api_auth_gateway.role_tasker_technical').id,
                request.env.ref('api_auth_gateway.role_tasker_stem').id,
                request.env.ref('api_auth_gateway.role_tasker_non_stem').id,
            ]

            team_role_message = ""
            team_member_ids = []
            if project.project_lead:
                team_member_ids.extend(project.project_lead.ids)
                team_role_message += f"{len(project.project_lead)}, PL"

            if project.project_qc_reviewer:
                team_role_message += f"{len(project.project_qc_reviewer)}, QR"
                team_member_ids.extend(project.project_qc_reviewer.ids)

            if project.project_tasker:
                team_role_message += f"{len(project.project_tasker)}, Tasker"
                team_member_ids.extend(project.project_tasker.ids)

            if project.project_aire:
                team_role_message += f"{len(project.project_aire)}, AIRE"
                team_member_ids.extend(project.project_aire.ids)

            if project.project_swe:
                team_role_message += f"{len(project.project_swe)}, SWE"
                team_member_ids.extend(project.project_swe.ids)

            tpm_ids = list(set(project.project_lead.mapped('task_forge_tpm_id').ids))
            if tpm_ids:
                team_role_message += f"{len(tpm_ids)}, TPM"
                team_member_ids.extend(tpm_ids)

            state_list = ['in_progress', 'completed', 'blocker', 'returned', 'ack', 'escalated', 'overdue']
            task_progress_grouped = TaskLog.read_group(
                domain=[('project_id', '=', project_id)],
                fields=['state'],
                groupby=['state']
            )
            state_totals = {item['state']: item['state_count'] for item in task_progress_grouped}
            task_progress = [
                {'state': state, 'total_count': state_totals.get(state, 0)}
                for state in state_list
            ]

            days = int(kwargs.get('days', 7))
            end_date = datetime.datetime.now().date()
            start_date = end_date - datetime.timedelta(days=days - 1)

            throughput_domain = [
                ('project_id', '=', project_id),
                ('state', '=', 'completed'),
                ('end_time', '>=', start_date),
                ('end_time', '<=', end_date + datetime.timedelta(days=1))
            ]
            throughput_grouped = TaskLog.read_group(
                domain=throughput_domain,
                fields=['end_time'],
                groupby=['end_time:day']
            )
            daily_throughput = [
                {'date': item['end_time:day'], 'completed_count': item['end_time_count']}
                for item in throughput_grouped if item['end_time_count']
            ]

            return return_Response(
                message="Project dashboard",
                status=200,
                data={
                    'name': project.name if project.name else "",
                    'project_seq': project.project_seq if project.project_seq else "",
                    'client_name': project.client_name if project.client_name else "",
                    'status': project.non_stemp_project_status if project.project_category == 'non_stem' else project.stage_id.name,
                    'category': project.project_category if project.project_category else "",
                    'project_classification': project.project_classification if project.project_classification else "",
                    'date_start': str(project.date_start) if project.date_start else "",
                    'date_end': str(project.date) if project.date else "",
                    'done_task_count': done_task_count,
                    'this_week_new_task_count': this_week_new_task_count,
                    'total_task_count': total_task_count,
                    'completion_rate': f"{completion_rate}%",
                    'open_blocker_count': open_blocker_count,
                    'blocker_by_state': blocker_message,
                    'total_team_size': len(set(team_member_ids)),
                    'team_role_message': team_role_message,
                    'task_progress': task_progress,
                    'daily_throughput': daily_throughput,
                    'tab_list': [{'tab_name': tab.table_name or "", 'api_end_point': tab.api_prefix or ""} for tab in project.api_map_ids],
                    'category_url': project.category_url if project.category_url else "",
                    'tasker_url': project.tasker_url if project.tasker_url else "",
                }
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/project/team_analytics', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'project_id': {'type': 'str', 'required': True},
    })
    def get_team_analytics(self, **kwargs):
        try:
            project_id = int(kwargs.get('project_id'))
            project = request.env['project.project'].sudo().browse(project_id)
            if not project.exists():
                return return_Response(message="Project not found", status=404)

            Employee = request.env['hr.employee'].sudo()
            Attendance = request.env['hr.attendance'].sudo()
            team_member_ids = []
            if project.project_lead:
                team_member_ids.extend(project.project_lead.ids)

            if project.project_qc_reviewer:
                team_member_ids.extend(project.project_qc_reviewer.ids)

            if project.project_tasker:
                team_member_ids.extend(project.project_tasker.ids)

            if project.project_aire:
                team_member_ids.extend(project.project_aire.ids)

            if project.project_swe:
                team_member_ids.extend(project.project_swe.ids)

            if not team_member_ids:
                return return_Response(
                    message="Team analytics",
                    status=200,
                    data={
                        'project_lead_count': 0,
                        'qc_reviewer_count': 0,
                        'qc_lead_count': 0,
                        'tasker_trainee_count': 0,
                        'tasker_permanent_count': 0,
                        'active_member_count': 0,
                    }
                )

            role_pl = [
                request.env.ref('api_auth_gateway.role_pl_technical').id,
                request.env.ref('api_auth_gateway.role_pl_stem').id,
                request.env.ref('api_auth_gateway.role_pl_non_stem').id,
            ]
            role_qr = [
                request.env.ref('api_auth_gateway.role_qc_technical').id,
                request.env.ref('api_auth_gateway.role_qc_stem').id,
                request.env.ref('api_auth_gateway.role_qc_non_stem').id,
            ]
            role_ql = [
                request.env.ref('api_auth_gateway.role_qc_technical').id,
                request.env.ref('api_auth_gateway.role_qc_stem').id,
                request.env.ref('api_auth_gateway.role_qc_non_stem').id,
            ]
            role_tasker = [
                request.env.ref('api_auth_gateway.role_tasker_technical').id,
                request.env.ref('api_auth_gateway.role_tasker_stem').id,
                request.env.ref('api_auth_gateway.role_tasker_non_stem').id,
            ]

            project_lead_count = Employee.search_count([
                ('id', 'in', team_member_ids),
                ('user_id.user_role', 'in', role_pl)
            ])

            qc_employees = Employee.search([
                ('id', 'in', team_member_ids),
                ('user_id.user_role', 'in', role_ql)
            ])

            qc_reviewer_count = qc_employees.filtered(lambda e: e.user_id.user_role.id in role_qr and e._get_task_forge_role() == 'qr')
            qc_lead_count = qc_employees.filtered(lambda e: e.user_id.user_role.id in role_ql and e._get_task_forge_role() == 'ql')

            tasker_trainee_count = Employee.search_count([
                ('id', 'in', team_member_ids),
                ('user_id.user_role', 'in', role_tasker),
                ('tasker_status', '=', 'trainee')
            ])
            tasker_permanent_count = Employee.search_count([
                ('id', 'in', team_member_ids),
                ('user_id.user_role', 'in', role_tasker),
                ('tasker_status', '=', 'permanent')
            ])

            today = datetime.datetime.now().date()
            active_member_ids = Attendance.search([
                ('check_in', '>=', f"{today} 00:00:00"),
                ('employee_id', 'in', team_member_ids),
                ('attendance_status', '=', 'present')
            ]).mapped('employee_id').ids
            active_member_count = len(set(active_member_ids))

            return return_Response(
                message="Team analytics",
                status=200,
                data={
                    'project_lead_count': project_lead_count,
                    'qc_reviewer_count': len(qc_reviewer_count),
                    'qc_lead_count': len(qc_lead_count),
                    'tasker_trainee_count': tasker_trainee_count,
                    'tasker_permanent_count': tasker_permanent_count,
                    'active_member_count': active_member_count,
                }
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _format_project(self, project):
        has_alloc = bool(request.env['task.forge.allocation'].sudo().search_count([
            ('project_id', '=', project.id)
        ]))
        return {
            'id': project.id,
            'name': project.name,
            'status': project.task_forge_status or 'live',
            'platform': project.task_forge_platform or 'Multimango',
            'is_allocated': has_alloc,
            'created_at': project.create_date.isoformat() if project.create_date else '',
        }

    def _format_allocation(self, alloc):
        return {
            'id': alloc.id,
            'project_id': alloc.project_id.id,
            'project_name': alloc.project_id.name,
            'project_status': alloc.project_id.task_forge_status or 'live',
            'employee_id': alloc.employee_id.id,
            'employee_name': alloc.employee_id.name,
            'allocated_by': alloc.allocated_by_id.name if alloc.allocated_by_id else '',
            'is_tested': alloc.is_tested,
            'is_visible_on_multimango': alloc.is_visible_on_multimango,
            'created_at': alloc.create_date.isoformat() if alloc.create_date else '',
        }

    @http.route('/api/v2/project_team_member_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'project_id': {'type': 'str', 'required': True},
    })
    def project_team_member_list(self, **kwargs):
        try:
            # 1. Get project
            project_id = int(kwargs.get('project_id'))
            project = request.env['project.project'].sudo().browse(project_id)

            if not project.exists():
                return return_Response(message="Project not found", status=404)

            # 2. Get team members
            team_ids = project.project_lead | project.project_qc_reviewer | project.project_tasker

            cto_role = request.env.ref('api_auth_gateway.role_cto_technical', raise_if_not_found=False)
            tpm_role = request.env.ref('api_auth_gateway.role_tpm_technical', raise_if_not_found=False)
            allowed_role_ids = [r.id for r in (cto_role, tpm_role) if r]
            caller_role_id = request.env.user.user_role.id if request.env.user.user_role else False
            if caller_role_id and caller_role_id in allowed_role_ids:
                team_ids = team_ids | project.project_lead.mapped('task_forge_tpm_id')

            # 3. Build domain (IMPORTANT FIX)
            domain = [('id', 'in', team_ids.ids)]

            # Active filter
            if kwargs.get('active') == 'false':
                domain.append(('task_forge_active', '=', False))
            else:
                domain.append(('task_forge_active', '=', True))

            # Search filter
            if kwargs.get('search'):
                domain.append(('name', 'ilike', kwargs.get('search')))

            # Role filter
            if kwargs.get('role'):
                domain.append(('user_id.user_role.id', '=', int(kwargs.get('role'))))

            # 4. Apply domain (IMPORTANT FIX)
            employees = request.env['hr.employee'].sudo().search(domain)

            # Per-member "tasks done" + avg duration. For internal projects the
            # truth lives in the project's connected backend model (e.g.
            # vegeta.job), NOT task.forge.log — which is empty for those
            # projects. When that backend exposes get_member_task_stats(user_id)
            # we use it per member; projects without a connected backend keep the
            # legacy task.forge.log count.
            backend = None
            backend_name = project.connected_table
            if backend_name and backend_name in request.env:
                candidate = request.env[backend_name].sudo()
                if hasattr(candidate, 'get_member_task_stats'):
                    backend = candidate

            # Legacy fallback (projects with no connected backend): per-member
            # "done" count + average handling time from task.forge.log. Computed
            # once via read_group — not re-queried per employee — and keyed by
            # employee_id so each member gets their own number (the old code
            # counted the whole project for everyone). Uses the real duration
            # (time_taken_mins), NOT the free-text pause_time, and returns
            # seconds to match the backend hook's unit.
            legacy_stats = {}
            if backend is None:
                grouped = request.env['task.forge.log'].sudo().read_group(
                    domain=[('project_id', '=', project.id), ('state', '=', 'completed')],
                    fields=['employee_id', 'time_taken_mins:avg'],
                    groupby=['employee_id'],
                )
                for g in grouped:
                    emp_ref = g.get('employee_id')
                    if not emp_ref:
                        continue
                    legacy_stats[emp_ref[0]] = {
                        'done': g.get('employee_id_count', 0),
                        'avg_seconds': int((g.get('time_taken_mins') or 0) * 60),
                    }

            # 6. Build response
            data = []
            for emp in employees:
                if backend is not None and emp.user_id:
                    stats = backend.get_member_task_stats(emp.user_id.id) or {}
                else:
                    stats = legacy_stats.get(emp.id, {})
                total_done = stats.get('done', 0)
                avg_time = stats.get('avg_seconds', 0)

                data.append({
                    'id': emp.id,
                    'name': emp.name or "",
                    'email': emp.work_email or "",
                    'job_title_id': emp.designation_id.id if emp.designation_id else 0,
                    'job_title': emp.designation_id.name if emp.designation_id else "",
                    'department_id': emp.department_id.id if emp.department_id else 0,
                    'department': emp.department_id.name if emp.department_id else "",
                    'offboarding_state': emp.offboarding_state or "",
                    'role_id': emp.user_id.user_role.id if emp.user_id and emp.user_id.user_role else 0,
                    'role': emp.user_id.user_role.name if emp.user_id and emp.user_id.user_role else "",
                    'total_done_task': total_done,
                    'pl_id': emp.task_forge_pl_id.id if emp.task_forge_pl_id else 0,
                    'pl_name': emp.task_forge_pl_id.name if emp.task_forge_pl_id else "",
                    'qr_id': emp.task_forge_qr_id.id if emp.task_forge_qr_id else 0,
                    'qr_name': emp.task_forge_qr_id.name if emp.task_forge_qr_id else "",
                    'active': emp.active,
                    'avg_time': avg_time,
                    'since': str(emp.create_date.date()) if emp.create_date else ""
                })

            # 7. Final response
            return return_Response(
                message=f"{len(data)} employees found",
                status=200,
                data={
                    'data': data,
                    'total': len(employees)
                }
            )

        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/project/task_summary', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'project_id': {'type': 'str', 'required': True},
    })
    def project_task_summary(self, **kwargs):
        try:
            project_id = int(kwargs.get('project_id'))
            project = request.env['project.project'].sudo().browse(project_id)
            if not project.exists():
                return return_Response(message="Project not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()
            Blocker = request.env['task.forge.blocker'].sudo()

            task_domain = [('project_id', '=', project_id)]
            total_task_count = TaskLog.search_count(task_domain)
            done_task_count = TaskLog.search_count(task_domain + [('state', '=', 'completed')])
            in_progress_count = TaskLog.search_count(task_domain + [('state', '=', 'in_progress')])

            blocker_domain = [('project_id', '=', project_id), ('state', 'not in', ['no_issue', 'resolved'])]
            total_blocker_count = Blocker.search_count(blocker_domain)

            blocker_grouped = Blocker.read_group(
                domain=blocker_domain,
                fields=['state'],
                groupby=['state'],
            )
            blocker_message = ""
            for item in blocker_grouped:
                if item['state_count']:
                    blocker_message += f"{item['state_count']} {item['state']} "

            return return_Response(
                message="Project task summary",
                status=200,
                data={
                    'project_id': project_id,
                    'project_name': project.name or "",
                    'total_task_count': total_task_count,
                    'done_task_count': done_task_count,
                    'in_progress_count': in_progress_count,
                    'total_blocker_count': total_blocker_count,
                    'blocker_message': blocker_message,
                },
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/project/offboard_member', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'project_id': {'type': 'int', 'required': True},
        'employee_id': {'type': 'int', 'required': True},
        'reason_id': {'type': 'int', 'required': True},
    })
    def offboard_project_member(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            project_id = int(jdata.get('project_id'))
            employee_id = int(jdata.get('employee_id'))
            role = jdata.get('role', '')
            reason = jdata.get('reason', '')
            reason_id = int(jdata.get('reason_id')) if jdata.get('reason_id') else False
            notes = jdata.get('notes', '')
            end_date = jdata.get('end_date') or False

            project = request.env['project.project'].sudo().browse(project_id)
            if not project.exists():
                return return_Response(message="Project not found", status=404)

            employee = request.env['hr.employee'].sudo().browse(employee_id)
            if not employee.exists():
                return return_Response(message="Employee not found", status=404)

            History = request.env['project.member.history'].sudo()
            domain = [
                ('project_id', '=', project_id),
                ('employee_id', '=', employee_id),
                ('state', '=', 'active'),
            ]
            if role:
                domain.append(('role', '=', role))

            active_records = History.search(domain)
            if not active_records:
                return return_Response(message="No active assignment found for this employee on the project", status=404)

            role_to_field = {
                'lead': 'project_lead',
                'qc_reviewer': 'project_qc_reviewer',
                'tasker': 'project_tasker',
                'aire': 'project_aire',
                'swe': 'project_swe',
            }

            from odoo import fields as odoo_fields
            for rec in active_records:
                rec.write({
                    'state': 'offboarded',
                    'end_date': end_date or odoo_fields.Date.today(),
                    'offboard_reason': reason,
                    'reason_id': reason_id,
                    'notes': notes,
                })

                field_name = role_to_field.get(rec.role)
                if field_name and employee_id in project[field_name].ids:
                    project.write({field_name: [(3, employee_id)]})

            return return_Response(
                message="Employee offboarded from project successfully",
                status=200,
                data={
                    'project_id': project_id,
                    'employee_id': employee_id,
                    'offboarded_roles': [r.role for r in active_records],
                    'end_date': str(end_date or odoo_fields.Date.today()),
                }
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/project/member_history', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def project_member_history(self, **kwargs):
        try:
            project_id = kwargs.get('project_id')
            if not project_id:
                return return_Response(message="project_id is required", status=400)

            project = request.env['project.project'].sudo().browse(int(project_id))
            if not project.exists():
                return return_Response(message="Project not found", status=404)

            History = request.env['project.member.history'].sudo()
            domain = [('project_id', '=', int(project_id))]

            if kwargs.get('state'):
                domain.append(('state', '=', kwargs.get('state')))
            if kwargs.get('role'):
                domain.append(('role', '=', kwargs.get('role')))
            if kwargs.get('employee_id'):
                domain.append(('employee_id', '=', int(kwargs.get('employee_id'))))

            records = History.search(domain, order='start_date desc')
            data = []
            for r in records:
                data.append({
                    'id': r.id,
                    'project_id': r.project_id.id,
                    'project_name': r.project_id.name or '',
                    'employee_id': r.employee_id.id,
                    'employee_name': r.employee_id.name or '',
                    'role': r.role,
                    'start_date': str(r.start_date) if r.start_date else '',
                    'end_date': str(r.end_date) if r.end_date else '',
                    'state': r.state,
                    'offboard_reason': r.offboard_reason or '',
                    'reason_id': r.reason_id.id if r.reason_id else 0,
                    'reason_name': r.reason_id.name if r.reason_id else '',
                    'notes': r.notes or '',
                })

            return return_Response(
                message="Project member history",
                status=200,
                data={'data': data}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/project/hierarchy', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def project_hierarchy(self, **kwargs):
        try:
            Employee = request.env['hr.employee'].sudo()
            Project = request.env['project.project'].sudo()

            domain = []
            if kwargs.get('project_id'):
                domain.append(('id', '=', int(kwargs.get('project_id'))))
            if kwargs.get('search'):
                domain.append(('name', 'ilike', kwargs.get('search')))
            projects = Project.search(domain)
            result = []
            emp_list = []

            unassigned_employee = []
            for proj in projects:
                pl_employees = proj.project_lead
                qr_employees = proj.project_qc_reviewer
                tasker_employees = proj.project_tasker
                emp_list.extend(pl_employees.ids)
                emp_list.extend(qr_employees.ids)
                emp_list.extend(tasker_employees.ids)

                qr_ids_set = set(qr_employees.ids)
                tasker_ids_set = set(tasker_employees.ids)

                pl_data = []
                assigned_qr_ids = set()
                assigned_tasker_ids = set()

                for pl in pl_employees:
                    pl_qrs = Employee.search([
                        ('task_forge_pl_id', '=', pl.id),
                        ('id', 'in', list(qr_ids_set)),
                    ])
                    assigned_qr_ids.update(pl_qrs.ids)

                    qr_data = []
                    for qr in pl_qrs:
                        qr_taskers = Employee.search([
                            ('task_forge_qr_id', '=', qr.id),
                            ('id', 'in', list(tasker_ids_set)),
                        ])
                        assigned_tasker_ids.update(qr_taskers.ids)

                        qr_data.append({
                            'id': qr.id,
                            'name': qr.name or '',
                            'role': qr.user_id.user_role.name,
                            'tasker_count': len(qr_taskers),
                            'taskers': [{
                                'id': t.id,
                                'name': t.name or '',
                                'role': t.user_id.user_role.name or '',
                            } for t in qr_taskers],
                        })

                    pl_data.append({
                        'id': pl.id,
                        'name': pl.name or '',
                        'role': pl.user_id.user_role.name or '',
                        'qr_count': len(pl_qrs),
                        'tasker_count': sum(q['tasker_count'] for q in qr_data),
                        'qrs': qr_data,
                    })

                result.append({
                    'project_id': proj.id,
                    'project_name': proj.name or '',
                    'project_seq': proj.project_seq or '',
                    'pl_count': len(pl_employees),
                    'qr_count': len(qr_employees),
                    'tasker_count': len(tasker_employees),
                    'pls': pl_data
                })
            employee_list = request.env['hr.employee'].sudo().search([('id', 'not in', emp_list), ('user_id.user_role.project_type', '=', request.env.user.user_role.project_type)])
            for el in employee_list:
                unassigned_employee.append({
                        'id': el.id,
                        'name': el.name or '',
                        'role': el.user_id.user_role.name or '',
                    })

            return return_Response(
                message="Project hierarchy",
                status=200,
                data={'data': result, 'unassigned_employee': unassigned_employee}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/projects/release_member', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'project_id': {'type': 'int', 'required': True},
        'employee_id': {'type': 'int', 'required': True},
    })
    def release_member(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            user_role_id = user.user_role.id if user.user_role else 0
            cto_ids = [request.env.ref(r).id for r in CTO_ROLE_REFS]
            tpm_ids = [request.env.ref(r).id for r in TPM_ROLE_REFS]
            pl_ids = [request.env.ref(r).id for r in PL_ROLE_REFS]
            qr_ids = [request.env.ref(r).id for r in QR_ROLE_REFS]
            actor_is_cto = user_role_id in cto_ids
            actor_is_tpm = user_role_id in tpm_ids
            actor_is_pl = user_role_id in pl_ids
            actor_is_qr = user_role_id in qr_ids

            if not (actor_is_cto or actor_is_tpm or actor_is_pl or actor_is_qr):
                return return_Response(
                    message="Permission denied: only CTO, TPM, PL or QR can release a project member",
                    status=403,
                )

            Project = request.env['project.project'].sudo()
            Employee = request.env['hr.employee'].sudo()

            project = Project.browse(int(jdata['project_id']))
            if not project.exists():
                return return_Response(message="Project not found", status=404)

            employee = Employee.browse(int(jdata['employee_id']))
            if not employee.exists():
                return return_Response(message="Employee not found", status=404)
            employee_id = employee.id

            explicit_role = (jdata.get('role') or '').strip().lower() or None
            if explicit_role and explicit_role not in ('tasker', 'qr', 'pl'):
                return return_Response(message="role must be one of: tasker, qr, pl", status=400)

            is_tasker = employee_id in project.project_tasker.ids
            is_qr = employee_id in project.project_qc_reviewer.ids
            is_pl = employee_id in project.project_lead.ids

            if not (is_tasker or is_qr or is_pl):
                return return_Response(message="Employee is not a member of this project", status=404)

            if explicit_role:
                holds = {'tasker': is_tasker, 'qr': is_qr, 'pl': is_pl}
                if not holds[explicit_role]:
                    return return_Response(
                        message=f"Employee is not a {explicit_role.upper()} on this project",
                        status=400,
                    )
                role = explicit_role
            else:
                roles_held = [r for r, ok in (('tasker', is_tasker), ('qr', is_qr), ('pl', is_pl)) if ok]
                if len(roles_held) > 1:
                    return return_Response(
                        message=f"Employee holds multiple roles on this project: {roles_held}. Pass 'role' explicitly.",
                        status=400,
                    )
                role = roles_held[0]

            if not (actor_is_cto or actor_is_tpm):
                if actor_is_pl:
                    if role == 'pl':
                        return return_Response(
                            message="Permission denied: PL cannot release another PL",
                            status=403,
                        )
                elif actor_is_qr:
                    if role != 'tasker':
                        return return_Response(
                            message="Permission denied: QR can only release a Tasker",
                            status=403,
                        )
                else:
                    return return_Response(message="Permission denied", status=403)

            reassigned = []

            if role == 'tasker':
                project.sudo().write({'project_tasker': [(3, employee_id)]})
            elif role == 'qr':
                remaining_qr_ids = [eid for eid in project.project_qc_reviewer.ids if eid != employee_id]
                if not remaining_qr_ids:
                    return return_Response(
                        message="No other QR on this project. Assign another QR before releasing this one.",
                        status=400,
                    )
                affected = project.project_tasker.filtered(lambda t: t.task_forge_qr_id.id == employee_id)
                for i, tasker in enumerate(affected):
                    new_qr_id = remaining_qr_ids[i % len(remaining_qr_ids)]
                    tasker.sudo().write({'task_forge_qr_id': new_qr_id})
                    reassigned.append({
                        'tasker_id': tasker.id,
                        'tasker_name': tasker.name or '',
                        'new_qr_id': new_qr_id,
                    })
                project.sudo().write({'project_qc_reviewer': [(3, employee_id)]})
            else:
                remaining_pl_ids = [eid for eid in project.project_lead.ids if eid != employee_id]
                if not remaining_pl_ids:
                    return return_Response(
                        message="No other PL on this project. Assign another PL before releasing this one.",
                        status=400,
                    )
                affected_taskers = project.project_tasker.filtered(lambda t: t.task_forge_pl_id.id == employee_id)
                affected_qrs = project.project_qc_reviewer.filtered(lambda q: q.task_forge_pl_id.id == employee_id)
                affected = [('tasker', emp) for emp in affected_taskers] + [('qr', emp) for emp in affected_qrs]
                for i, (member_role, emp) in enumerate(affected):
                    new_pl_id = remaining_pl_ids[i % len(remaining_pl_ids)]
                    emp.sudo().write({'task_forge_pl_id': new_pl_id})
                    reassigned.append({
                        'employee_id': emp.id,
                        'employee_name': emp.name or '',
                        'member_role': member_role,
                        'new_pl_id': new_pl_id,
                    })
                project.sudo().write({'project_lead': [(3, employee_id)]})

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'Project Member Released',
                    'message': f'{employee.name} released from "{project.name}" ({role.upper()})',
                    'user_id': request.env.user.id,
                    'priority': '1',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass

            return return_Response(
                message=f"Member released as {role.upper()}",
                status=200,
                data={'data': {
                    'project_id': project.id,
                    'employee_id': employee.id,
                    'employee_name': employee.name or '',
                    'role': role,
                    'reassigned_taskers': reassigned,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)
