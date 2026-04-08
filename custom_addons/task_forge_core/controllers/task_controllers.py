from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request, generate_s3_link
)
from datetime import datetime, date, timedelta
import json
import base64


class TaskForgeTaskController(http.Controller):

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

            # 4. Search Logic (The prefix notation fix)
            search_val = kwargs.get('search')
            if search_val:
                # We add a '|' for the two following conditions
                domain.extend(['|', ('employee_id.name', 'ilike', search_val), ('name', 'ilike', search_val)])

            tasks = TaskLog.search(domain, order='create_date desc', limit=int(kwargs.get('limit', 200)))
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
            return return_Response(message="Task created", status=200, data={'data': self._format_task(task)})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _format_task(self, task):
        return {
            'id': task.id if task.id else 0,
            'sequence': task.sequence if task.sequence else "",
            'task_name': task.name if task.name else "",
            'employee_id': task.employee_id.id if task.employee_id.id else 0,
            'employee_name': task.employee_id.name if task.employee_id.name else "",
            'project_id': task.project_id.id if task.project_id else 0,
            'project_name': task.project_id.name if task.project_id.name else "",
            'date': str(task.date) if task.date else '',
            'status': task.state or "",
            'start_time': task.start_time.isoformat() if task.start_time else "",
            'end_time': task.end_time.isoformat() if task.end_time else "",
            'pause_time': task.pause_time if task.pause_time else "",
            'time_taken_mins': task.time_taken_mins or 0,
            'start_screenshot_url': task.start_screenshot_url or '',
            'end_screenshot_url': task.end_screenshot_url or '',
            'blocker_reason': task.blocker_reason or '',
            'quality_score': task.quality_score or 0,
            'prompt_justification': task.prompt_justification or '',
            'feedback_note': task.feedback_note or '',
            'created_at': task.create_date.isoformat() if task.create_date else '',
            'image_url_lines': [task.image_url for task in task.image_url_lines if task.image_url]
        }
