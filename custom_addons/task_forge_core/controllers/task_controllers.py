from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request, generate_s3_link
)
from datetime import datetime, date, timedelta
import json
import base64


class TaskForgeTaskController(http.Controller):

    @http.route('/api/v2/taskforge/tasks_group_by_project', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def tasks_group_by_project(self, **kwargs):
        try:
            user_id = request.env.user
            employee = user_id.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)
            team_ids = employee._get_team_employee_ids()

            domain = [('non_stemp_project_status', 'in', ['not_started', 'production'])]
            if kwargs.get('show_all') in [1, '1']:
                domain = []
            if user_id.user_role.id == request.env.ref('api_auth_gateway.role_cto_technical').id:
                domain = []
            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_pl_technical').id,
                                          request.env.ref('api_auth_gateway.role_pl_stem').id,
                                          request.env.ref('api_auth_gateway.role_pl_non_stem').id]:
                domain.append(('project_lead', '=', employee.id))
            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_qc_technical').id,
                                          request.env.ref('api_auth_gateway.role_qc_stem').id,
                                          request.env.ref('api_auth_gateway.role_qc_non_stem').id]:
                domain.append(('project_qc_reviewer', '=', employee.id))
            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_tasker_technical').id,
                                          request.env.ref('api_auth_gateway.role_tasker_stem').id,
                                          request.env.ref('api_auth_gateway.role_tasker_non_stem').id]:
                domain.append(('project_tasker', '=', employee.id))

            search = kwargs.get('search')
            if search:
                domain += ['|', ('name', 'ilike', search), ('internal_project_name', 'ilike', search)]

            if kwargs.get('project_status'):
                if 'all' not in kwargs.get('project_status'):
                    status_list = [int(x.strip()) for x in kwargs.get('project_status').split(',') if x.strip()]
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
            TaskLog = request.env['task.forge.log'].sudo()
            for p in projects:
                project_vals = {
                    'name': p.name or "",
                    'task_count': 0,
                    'aht_time': 0,
                    'task_list': []
                }
                task_domain = [('project_id', '=', p.id), ('employee_id', 'in', team_ids)]
                if kwargs.get('status'):
                    task_domain.append(('state', '=', kwargs.get('status')))
                tasks = TaskLog.search(task_domain, order='create_date desc')
                aht_time = 0
                for t in tasks:
                    project_vals.get('task_list').append(self._format_task(t))
                    if t.pause_time:
                        try:
                            aht_time += int(t.pause_time)
                        except (ValueError, TypeError):
                            pass
                aht_time = aht_time // 60 if aht_time else 0
                project_vals['aht_time'] = aht_time
                project_data.append(project_vals)
            return return_Response(message="Tasks list", status=200, data={'data': project_data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/tasks', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_tasks(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()
            TaskLog = request.env['task.forge.log'].sudo()

            domain = [('employee_id', 'in', team_ids)]

            if kwargs.get('employee_id'):
                domain.append(('employee_id', '=', int(kwargs['employee_id'])))

            if kwargs.get('project_id'):
                domain.append(('project_id', '=', int(kwargs['project_id'])))

            if kwargs.get('project'):
                domain.append(('project_id', '=', int(kwargs['project'])))

            if kwargs.get('status'):
                if kwargs.get('status') != 'all':
                    domain.append(('state', '=', kwargs['status']))

            today = date.today()
            if kwargs.get('date'):
                domain.append(('date', '=', kwargs.get('date')))

            filter_type = kwargs.get('filter')

            if filter_type == 'today':
                domain.append(('date', '=', today))

            elif filter_type == 'this_week':
                start_date = today - timedelta(days=7)
                domain.extend([('date', '>=', start_date), ('date', '<=', today)])

            elif filter_type == 'this_month':
                start_date = today - timedelta(days=30)
                domain.extend([('date', '>=', start_date), ('date', '<=', today)])

            if kwargs.get('start_date') and kwargs.get('end_date'):
                if kwargs['start_date'] == kwargs['end_date']:
                    domain.append(('date', '=', kwargs['start_date']))
                else:
                    domain.append(('date', '>=', kwargs['start_date']))
                    domain.append(('date', '<=', kwargs['end_date']))

            # 4. Search Logic (The prefix notation fix)
            search_val = kwargs.get('search')
            if search_val:
                # We add a '|' for the two following conditions
                domain.extend(['|', '|', ('employee_id.name', 'ilike', search_val), ('name', 'ilike', search_val), ('project_id.name', 'ilike', search_val)])

            tasks = TaskLog.search(domain, order='create_date desc')
            data = [self._format_task(t) for t in tasks]
            return return_Response(message="Tasks list", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/tasks/today', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def tasks_today(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()
            tasks = TaskLog.search([
                ('employee_id', '=', employee.id),
                ('date', '=', date.today()),
            ], order='create_date desc')

            data = [self._format_task(t) for t in tasks]
            return return_Response(message="Today's tasks", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/tasks/active', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def active_task(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()
            tasks = TaskLog.search([
                ('employee_id', '=', employee.id),
                ('state', '=', 'in_progress'),
            ])

            if not tasks:
                return return_Response(message="No active task", status=200, data={'data': None})

            return return_Response(message="Active task", status=200, data={'data': [self._format_task(task) for task in tasks]})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/tasks/start', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def start_task(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()

            # Check punch-in
            TaskLog._check_punch_in(employee.id)
            # Check no active task
            # TaskLog._check_no_active_task(employee.id)

            vals = {
                'name': kwargs.get('task_name'),
                'employee_id': employee.id,
                'date': date.today(),
                'state': 'in_progress',
                'start_time': datetime.now(),
            }
            if kwargs.get('project_id'):
                vals['project_id'] = int(kwargs.get('project_id'))

            # Handle start screenshot
            screenshot_file = request.httprequest.files.get('start_screenshot')
            if screenshot_file:
                img_data = base64.b64encode(screenshot_file.read())
                url = generate_s3_link(img_data, prefix='taskforge/screenshots', uid=employee.id)
                vals['start_screenshot_url'] = url
                vals['image_url_lines'] = [(0, 0, {'image_url': url, 'image_type': 'start'})]

            task = TaskLog.create(vals)
            if task.project_id:
                if task.project_id.non_stemp_project_status in ['not_started', 'draft']:
                    task.project_id.non_stemp_project_status = 'production'
                    task.project_id.sudo().write({
                        'stage_id': request.env.ref('project_extension.project_project_stage_ethara_10').id,
                        'non_stemp_project_status': 'production'
                    })

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'Task Started',
                    'message': f'{employee.name} started task "{task.name}".',
                    'user_id': request.env.user.id,
                    'priority': '1',
                    'res_model': 'task.forge.log',
                    'res_id': task.id,
                    'project_id': task.project_id.id if task.project_id else False,
                })
            except Exception:
                pass

            return return_Response(message="Task started", status=200, data={'data': self._format_task(task)})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/tasks/end', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def end_task(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()
            task = TaskLog.browse(int(kwargs.get('task_id')))
            if not task.exists():
                return return_Response(message="Task not found", status=404)
            if task.employee_id.id != employee.id:
                return return_Response(message="Not your task", status=403)
            if task.state != 'in_progress':
                return return_Response(message="Task is not in progress", status=400)
            if kwargs.get('pause_time'):
                task.pause_time = kwargs.get('pause_time')
            # Handle end screenshot
            end_screenshot_url = None
            screenshot_file = request.httprequest.files.get('end_screenshot')
            if screenshot_file:
                img_data = base64.b64encode(screenshot_file.read())
                end_screenshot_url = generate_s3_link(img_data, prefix='taskforge/screenshots', uid=employee.id)
            # elif jdata.get('end_screenshot'):
            #     end_screenshot_url = generate_s3_link(jdata['end_screenshot'], prefix='taskforge/screenshots', uid=employee.id)

            result = task.action_end(
                end_screenshot_url=end_screenshot_url,
                blocker_reason=kwargs.get('blocker_reason'),
            )

            task.invalidate_recordset()

            try:
                msg = f'Task "{task.name}" has been completed.' if task.state == 'completed' else f'Blocker raised on task "{task.name}".'
                request.env['kubera.notification'].sudo().create({
                    'title': 'Task Completed' if task.state == 'completed' else 'Blocker Raised',
                    'message': msg,
                    'user_id': request.env.user.id,
                    'priority': '2' if task.state == 'blocker' else '1',
                    'res_model': 'task.forge.log',
                    'res_id': task.id,
                    'project_id': task.project_id.id if task.project_id else False,
                })
            except Exception:
                pass

            return return_Response(
                message="Task ended" if task.state == 'completed' else "Blocker raised",
                status=200,
                data={'data': self._format_task(task)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/tasks/pause', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({"task_id": {"type": "str", "required": True}})
    def pause_task(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()
            task = TaskLog.browse(int(kwargs.get('task_id')))
            if not task.exists():
                return return_Response(message="Task not found", status=404)
            if task.state != 'in_progress':
                return return_Response(message="Task is not in progress", status=400)
            end_screenshot_url = None
            screenshot_file = request.httprequest.files.get('screenshot')
            if screenshot_file:
                img_data = base64.b64encode(screenshot_file.read())
                end_screenshot_url = generate_s3_link(img_data, prefix='taskforge/screenshots', uid=employee.id)
                task.image_url_lines = [(0, 0, {'image_url': end_screenshot_url, 'image_type': 'start'})]
            if kwargs.get('pause_time'):
                task.pause_time = kwargs.get('pause_time')

            return return_Response(
                message="Task Paused",
                status=200,
                data={'data': self._format_task(task)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/tasks/rating', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({"task_id": {"type": "str", "required": True}})
    def rate_task(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()
            task = TaskLog.browse(int(kwargs.get('task_id')))
            if not task.exists():
                return return_Response(message="Task not found", status=404)
            if task.state != 'in_progress':
                return return_Response(message="Task is not in progress", status=400)
            if kwargs.get('rating'):
                task.quality_score = kwargs.get('rating')
            if kwargs.get('comment'):
                task.comment = kwargs.get('comment')

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'Task Rated',
                    'message': f'Task "{task.name}" has been rated.',
                    'user_id': request.env.user.id,
                    'priority': '1',
                    'res_model': 'task.forge.log',
                    'res_id': task.id,
                    'project_id': task.project_id.id if task.project_id else False,
                })
            except Exception:
                pass

            return return_Response(
                message="Task Rated",
                status=200,
                data={'data': self._format_task(task)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/tasks/create', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'task_name': {'type': 'string', 'required': True},
        'employee_id': {'type': 'int', 'required': False},
        'project_id': {'type': 'int', 'required': False},
        'date': {'type': 'date', 'required': False},
        'time_taken_mins': {'type': 'int', 'required': False},
        'status': {'type': 'string', 'required': False},
    })
    def create_task(self, jdata=None, **kwargs):
        """Backdoor task creation for bulk logging."""
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()
            now = datetime.now()

            vals = {
                'name': jdata['task_name'],
                'employee_id': jdata.get('employee_id', employee.id),
                'date': jdata.get('date', str(date.today())),
                'state': jdata.get('status', 'completed'),
                'start_time': now,
                'end_time': now,
            }
            if jdata.get('project_id'):
                vals['project_id'] = jdata['project_id']

            task = TaskLog.create(vals)

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'Task Created',
                    'message': f'Task "{task.name}" has been created.',
                    'user_id': request.env.user.id,
                    'priority': '1',
                    'res_model': 'task.forge.log',
                    'res_id': task.id,
                    'project_id': task.project_id.id if task.project_id else False,
                })
            except Exception:
                pass

            return return_Response(message="Task created", status=200, data={'data': self._format_task(task)})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _format_task(self, task):
        # hours, mins = divmod(int(round(task.time_taken_mins)), 60)
        # duration_display = f"{hours:02d}:{mins:02d}"
        IST_OFFSET = timedelta(hours=5, minutes=30)
        start_time = (task.start_time + IST_OFFSET) if task.start_time else ""
        end_time = (task.end_time + IST_OFFSET) if task.end_time else ""
        create_date = (task.create_date + IST_OFFSET) if task.create_date else ""
        state = {
            'in_progress': 'In Progress',
            'completed': 'Completed',
            'blocker': 'Blocker',
            'returned': 'Returned',
            'ack': 'Acknowledged',
            'escalated': 'Escalated',
            'overdue': 'Overdue'
            }
        TaskLog = request.env['task.forge.log'].sudo()
        tasks = TaskLog.search_count([('employee_id', '=', task.employee_id.id), ('state', 'not in', ['no_issue'])])
        Blocker = request.env['task.forge.blocker'].sudo().search([('task_id', '=', task.id)])
        return {
            'id': task.id if task.id else 0,
            'sequence': task.sequence if task.sequence else "",
            'task_name': task.name if task.name else "",
            'employee_id': task.employee_id.id if task.employee_id.id else 0,
            'employee_name': task.employee_id.name if task.employee_id.name else "",
            'project_id': task.project_id.id if task.project_id else 0,
            'project_name': task.project_id.name if task.project_id.name else "",
            'date': str(task.date) if task.date else '',
            'status': state.get(task.state) if task.state else "",
            'start_time': str(start_time),
            'end_time': str(end_time),
            'pause_time': task.pause_time if task.pause_time else "",
            'time_taken_mins': round(int(task.pause_time) / 60, 2) if task.pause_time else 0,
            # 'time_taken_mins': task.time_taken_mins or 0,
            # 'time_taken_mins': duration_display,
            'start_screenshot_url': task.start_screenshot_url or '',
            'end_screenshot_url': task.end_screenshot_url or '',
            'blocker_reason': ", ".join(Blocker.mapped('name')) if Blocker else "",
            'blocker_count': len(Blocker),
            'blocker_status': state.get(Blocker.mapped('state')[0]) or "" if Blocker else "",
            'quality_score': task.quality_score or 0,
            'prompt_justification': task.prompt_justification or '',
            'feedback_note': task.feedback_note or '',
            'created_at': str(create_date),
            'image_url_lines': [task.image_url for task in task.image_url_lines if task.image_url]
        }

    @http.route('/api/v2/taskforge/tasks/delete', methods=['DELETE'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({'task_id': {'type': 'int', 'required': True}})
    def delete_task(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            task_id = int(jdata.get('task_id'))

            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            task = request.env['task.forge.log'].sudo().browse(task_id)
            if not task.exists():
                return return_Response(message="Task not found", status=404)

            role = employee._get_task_forge_role()
            if role == 'tasker' and task.employee_id.id != employee.id:
                return return_Response(message="You can only delete your own tasks", status=403)
            elif role in ('qr', 'ql'):
                team_ids = employee._get_team_employee_ids()
                if task.employee_id.id not in team_ids:
                    return return_Response(message="Access denied: task not in your team", status=403)

            task_name = task.name
            task_ref = task.sequence

            # Collect counts before deletion
            blocker_count = len(task.blocker_ids)
            bug_report_count = len(task.bug_report_ids)
            validated_bugs = request.env['task.forge.validated.bug'].sudo().search([('task_id', '=', task.id)])
            images = request.env['task.forge.image'].sudo().search([('task_id', '=', task.id)])

            deleted_counts = {
                'blockers': blocker_count,
                'bug_reports': bug_report_count,
                'validated_bugs': len(validated_bugs),
                'images': len(images),
            }

            # 1. Delete validated_bugs (ondelete='set null' — would be orphaned)
            for blockerr in task.blocker_ids:
                blockerr.sudo().unlink()

            if validated_bugs:
                validated_bugs.unlink()

            # 2. Delete images (no ondelete — would be orphaned)
            if images:
                images.unlink()

            # 3. Delete task — blockers and bug_reports cascade automatically
            task.unlink()

            return return_Response(
                message="Task deleted successfully",
                status=200)
        except Exception as e:
            return return_Response(message=str(e), status=400)
