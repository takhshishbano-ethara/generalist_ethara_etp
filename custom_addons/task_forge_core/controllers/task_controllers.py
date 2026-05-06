from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request, generate_s3_link
)
from odoo.addons.task_forge_core.services.kimi_client import call_kimi, parse_json_response
from datetime import datetime, date, timedelta
import json
import base64
import logging

_logger = logging.getLogger(__name__)

GRAMMAR_CHECK_SYSTEM_PROMPT = """You are an expert English language reviewer. Analyze the given text for grammar errors, spelling mistakes, punctuation issues, and clarity problems.

Return ONLY a valid JSON object. No markdown, no code fences, no extra text.

Schema:
{
  "is_correct": true/false,
  "issue_count": number,
  "corrected_text": "the complete input with all corrections applied",
  "issues": [
    {
      "category": "grammar|misspelling|punctuation|clarity|typography|capitalization|miscellaneous",
      "severity": "low|medium|high",
      "text": "exact problematic word/phrase from input",
      "suggestion_text": "correction",
      "message": "brief explanation"
    }
  ],
  "summary": {"grammar": 0, "misspelling": 0, "punctuation": 0, "clarity": 0, "typography": 0, "capitalization": 0, "miscellaneous": 0}
}

Rules:
- is_correct must be boolean true or false
- If text has ANY errors, is_correct MUST be false
- Every correction in issues MUST appear in corrected_text
- For long texts with many errors, list the top 30 most important issues
- Be thorough: flag every misspelling, every grammar error, every punctuation issue
"""


def _check_text_with_kimi(text):
    result = call_kimi(GRAMMAR_CHECK_SYSTEM_PROMPT, text, temperature=0.1)
    raw_text = result.get('text', '')
    _logger.info('Kimi raw response (%d chars): %s', len(raw_text), raw_text[:500])

    parsed = parse_json_response(raw_text)

    if not parsed:
        _logger.error('Failed to parse Kimi response as JSON. Raw: %s', raw_text[:1000])
        return {
            'original': text,
            'corrected': text,
            'is_correct': False,
            'error_percentage': 0,
            'issue_count': 0,
            'issues': [],
            'summary': {},
            'parse_error': 'Kimi returned a non-JSON response. Please try again.',
            'raw_response': raw_text[:500],
        }

    is_correct_raw = parsed.get('is_correct', False)
    if isinstance(is_correct_raw, str):
        is_correct = is_correct_raw.lower() == 'true'
    else:
        is_correct = bool(is_correct_raw)

    issue_count = int(parsed.get('issue_count', 0))
    if issue_count > 0:
        is_correct = False

    return {
        'original': text,
        'corrected': parsed.get('corrected_text', text),
        'is_correct': is_correct,
        'error_percentage': parsed.get('error_percentage', 0),
        'issue_count': issue_count,
        'issues': parsed.get('issues', []),
        'summary': parsed.get('summary', {}),
    }


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
            if kwargs.get('chain_of_thought'):
                vals['chain_of_thought'] = kwargs.get('chain_of_thought')

            # Handle start screenshot
            screenshot_file = request.httprequest.files.get('start_screenshot')
            if screenshot_file:
                img_data = base64.b64encode(screenshot_file.read())
                url = generate_s3_link(img_data, prefix='taskforge/screenshots', uid=employee.id)
                vals['start_screenshot_url'] = url
                vals['image_url_lines'] = [(0, 0, {'image_url': url, 'image_type': 'start'})]

            task = TaskLog.create(vals)

            # Scaffold empty response records from project config
            if task.project_id and task.project_id.is_response_required:
                request.env['task.forge.response'].sudo().scaffold_for_task(task)

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
            if kwargs.get('prompt'):
                task.prompt_text = kwargs.get('prompt')
            if kwargs.get('justification'):
                task.justification_text = kwargs.get('justification')
            if kwargs.get('chain_of_thought'):
                task.chain_of_thought = kwargs.get('chain_of_thought')

            rubric_validation_error = self._validate_and_stage_rubric_ratings(task, kwargs)
            if rubric_validation_error:
                return rubric_validation_error

            response_validation_error = self._validate_responses(task, kwargs)
            if response_validation_error:
                return response_validation_error

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

            if task.state == 'completed' and getattr(self, '_pending_rubric_vals', None):
                request.env['task.forge.rubric.rating'].sudo().create(self._pending_rubric_vals)
                self._pending_rubric_vals = None

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

    def _validate_and_stage_rubric_ratings(self, task, kwargs):
        """Parse rubric_ratings payload, validate, stash vals for create after action_end.

        Returns a Response on error, or None on success (sets self._pending_rubric_vals).
        """
        self._pending_rubric_vals = None
        if kwargs.get('blocker_reason'):
            return None

        project = task.project_id
        if not project or not project.is_rubrics_required:
            return None

        raw = kwargs.get('rubric_ratings')
        payload = []
        if raw:
            if isinstance(raw, str):
                try:
                    payload = json.loads(raw)
                except Exception:
                    return return_Response(
                        message="rubric_ratings must be valid JSON",
                        status=400,
                    )
            elif isinstance(raw, list):
                payload = raw
            else:
                return return_Response(
                    message="rubric_ratings must be a JSON list",
                    status=400,
                )

        if not isinstance(payload, list):
            return return_Response(
                message="rubric_ratings must be a JSON list",
                status=400,
            )

        Dimension = request.env['rubric.dimension'].sudo()
        Option = request.env['rubric.category.option'].sudo()

        project_dim_ids = set()
        required_dim_ids = set()
        for cat in project.rubric_category_ids:
            for dim in cat.dimension_ids:
                project_dim_ids.add(dim.id)
                if dim.is_required:
                    required_dim_ids.add(dim.id)

        seen_dims = set()
        vals_list = []
        for row in payload:
            if not isinstance(row, dict):
                return return_Response(
                    message="Each rubric rating must be an object",
                    status=400,
                )
            try:
                dim_id = int(row.get('dimension_id'))
                opt_id = int(row.get('option_id'))
            except (TypeError, ValueError):
                return return_Response(
                    message="dimension_id and option_id must be integers",
                    status=400,
                )

            if dim_id in seen_dims:
                return return_Response(
                    message=f"Duplicate rating for dimension {dim_id}",
                    status=400,
                )
            seen_dims.add(dim_id)

            if dim_id not in project_dim_ids:
                return return_Response(
                    message=f"Dimension {dim_id} does not belong to this project",
                    status=400,
                )

            dim = Dimension.browse(dim_id)
            opt = Option.browse(opt_id)
            if not dim.exists() or not opt.exists():
                return return_Response(
                    message=f"Unknown dimension or option (dim={dim_id}, opt={opt_id})",
                    status=400,
                )
            if opt.category_id.id != dim.category_id.id:
                return return_Response(
                    message=f"Option {opt_id} does not belong to dimension {dim_id}'s category",
                    status=400,
                )

            vals_list.append({
                'log_id': task.id,
                'dimension_id': dim_id,
                'option_id': opt_id,
            })

        missing = required_dim_ids - seen_dims
        if missing:
            return return_Response(
                message=f"Missing required dimensions: {sorted(missing)}",
                status=400,
            )

        self._pending_rubric_vals = vals_list
        return None

    def _validate_responses(self, task, kwargs):
        """Save responses if provided, then validate all are filled. Returns Response on error, None on success."""
        if kwargs.get('blocker_reason'):
            return None

        project = task.project_id
        if not project or not project.is_response_required:
            return None

        raw_responses = kwargs.get('responses')
        if raw_responses:
            payload = []
            if isinstance(raw_responses, str):
                try:
                    payload = json.loads(raw_responses)
                except Exception:
                    return return_Response(
                        message="responses must be valid JSON",
                        status=400,
                    )
            elif isinstance(raw_responses, list):
                payload = raw_responses

            if payload and isinstance(payload, list):
                Response = request.env['task.forge.response'].sudo()
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    config_id = item.get('config_id')
                    value = item.get('value', '')
                    if not config_id:
                        continue
                    rec = Response.search([
                        ('task_id', '=', task.id),
                        ('config_id', '=', int(config_id)),
                    ], limit=1)
                    if rec:
                        rec.write({'value': value})

                task.invalidate_recordset(['response_ids'])

        unfilled = task.response_ids.filtered(lambda r: not r.value)
        if unfilled:
            labels = ', '.join(unfilled.sorted('sequence').mapped('label'))
            return return_Response(
                message=f"All response fields must be filled before completing. Missing: {labels}",
                status=400,
            )
        return None

    @http.route('/api/v2/taskforge/tasks/responses', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'task_id': {'type': 'int', 'required': True},
        'responses': {'type': 'list', 'required': False},
        'chain_of_thought': {'type': 'string', 'required': False},
    })
    def save_responses(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()
            task = TaskLog.browse(int(jdata.get('task_id')))
            if not task.exists():
                return return_Response(message="Task not found", status=404)
            if task.employee_id.id != employee.id:
                return return_Response(message="Not your task", status=403)
            if task.state != 'in_progress':
                return return_Response(message="Task is not in progress", status=400)

            if jdata.get('chain_of_thought'):
                task.chain_of_thought = jdata.get('chain_of_thought')

            responses = jdata.get('responses', [])
            if not isinstance(responses, list):
                return return_Response(message="responses must be a list", status=400)

            Response = request.env['task.forge.response'].sudo()
            for item in responses:
                if not isinstance(item, dict):
                    return return_Response(message="Each response must be an object", status=400)
                config_id = item.get('config_id')
                value = item.get('value', '')
                if not config_id:
                    return return_Response(message="config_id is required in each response", status=400)

                rec = Response.search([
                    ('task_id', '=', task.id),
                    ('config_id', '=', int(config_id)),
                ], limit=1)
                if not rec:
                    return return_Response(
                        message=f"Response config {config_id} not found for this task",
                        status=404,
                    )
                rec.value = value or ''

            task.invalidate_recordset(['response_ids', 'response_completed'])

            return return_Response(
                message="Responses saved",
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
            if jdata.get('chain_of_thought'):
                vals['chain_of_thought'] = jdata['chain_of_thought']

            task = TaskLog.create(vals)

            if task.project_id and task.project_id.is_response_required:
                request.env['task.forge.response'].sudo().scaffold_for_task(task)

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
            'qc_approved': 'QC Approved',
            'qc_rejected': 'QC Rejected',
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
            'prompt': task.prompt_text if task.prompt_text else "",
            'justification': task.justification_text if task.justification_text else "",
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
            'is_justification_required': task.is_justification_required or False,
            'start_screenshot_url': task.start_screenshot_url or '',
            'end_screenshot_url': task.end_screenshot_url or '',
            'blocker_reason': ", ".join(Blocker.mapped('name')) if Blocker else "",
            'blocker_count': len(Blocker),
            'blocker_status': state.get(Blocker.mapped('state')[0]) or "" if Blocker else "",
            'quality_score': task.quality_score or 0,
            'prompt_justification': task.prompt_justification or '',
            'feedback_note': task.feedback_note or '',
            'created_at': str(create_date),
            'image_url_lines': [task.image_url for task in task.image_url_lines if task.image_url],
            'responses': [{
                'id': r.id,
                'config_id': r.config_id.id if r.config_id else 0,
                'label': r.label or '',
                'sequence': r.sequence or 0,
                'value': r.value or '',
            } for r in task.response_ids.sorted('sequence')],
            'response_completed': task.response_completed or False,
            'is_timer_enabled': task.project_id.is_timer_enabled if task.project_id else False,
	    'chain_of_thought': task.chain_of_thought or '',
            'task_score': task.task_score or 0,
            'comment': task.comment or '',
            'grammar_checked': task.grammar_checked or False,
            'grammar_is_perfect': task.grammar_is_perfect or False,
            'prompt_error_percentage': task.prompt_error_percentage or 0,
            'justification_error_percentage': task.justification_error_percentage or 0,
            'prompt_issue_count': task.prompt_issue_count or 0,
            'justification_issue_count': task.justification_issue_count or 0,
            'total_grammar_issues': task.total_grammar_issues or 0,
            'prompt_corrected': task.prompt_corrected or '',
            'justification_corrected': task.justification_corrected or '',
            'prompt_grammar_count': task.prompt_grammar_count or 0,
            'prompt_misspelling_count': task.prompt_misspelling_count or 0,
            'prompt_punctuation_count': task.prompt_punctuation_count or 0,
            'prompt_clarity_count': task.prompt_clarity_count or 0,
            'prompt_typography_count': task.prompt_typography_count or 0,
            'prompt_capitalization_count': task.prompt_capitalization_count or 0,
            'prompt_miscellaneous_count': task.prompt_miscellaneous_count or 0,
            'justification_grammar_count': task.justification_grammar_count or 0,
            'justification_misspelling_count': task.justification_misspelling_count or 0,
            'justification_punctuation_count': task.justification_punctuation_count or 0,
            'justification_clarity_count': task.justification_clarity_count or 0,
            'justification_typography_count': task.justification_typography_count or 0,
            'justification_capitalization_count': task.justification_capitalization_count or 0,
            'justification_miscellaneous_count': task.justification_miscellaneous_count or 0,
            'rubric_completed': task.rubric_completed or False,
            'rubric_ratings': [{
                'id': r.id,
                'dimension_id': r.dimension_id.id,
                'dimension_name': r.dimension_name_snapshot or (r.dimension_id.name or ''),
                'option_id': r.option_id.id,
                'option_name': r.option_name_snapshot or (r.option_id.name or ''),
                'option_value': r.option_value_snapshot or r.option_id.value,
                'category_id': r.category_id.id,
            } for r in task.rubric_rating_ids],
            'bug_reports': [{
                'id': b.id,
                'name': b.name or '',
                'state': b.state or '',
                'description': b.description if hasattr(b, 'description') else '',
            } for b in task.bug_report_ids],
            'qc_status': task.state if task.state in ('qc_approved', 'qc_rejected') else 'pending',
            'reviewed_by_id': task.reviewed_by_id.id if task.reviewed_by_id else 0,
            'reviewed_by_name': task.reviewed_by_id.name if task.reviewed_by_id else '',
            'review_date': str((task.review_date + IST_OFFSET)) if task.review_date else '',
            'rejection_reason': task.rejection_reason or '',
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

    @http.route('/api/v2/taskforge/tasks/grammar_check', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'prompt': {'type': 'str', 'required': True},
        'task_id': {'type': 'int', 'required': True}
    })
    def grammar_check(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            prompt = (jdata.get('prompt') or '').strip()
            justification = (jdata.get('justification') or '').strip()
            task = request.env['task.forge.log'].sudo().browse(int(jdata.get('task_id')))
            if not task.exists():
                return return_Response(message="Task not found", status=404)

            result = {'is_perfect': True, 'prompt': None, 'justification': None}

            if prompt:
                result['prompt'] = _check_text_with_kimi(prompt)
                if not result['prompt']['is_correct']:
                    result['is_perfect'] = False

            if justification:
                result['justification'] = _check_text_with_kimi(justification)
                if not result['justification']['is_correct']:
                    result['is_perfect'] = False

            p_data = result.get('prompt') or {}
            j_data = result.get('justification') or {}
            total_issues = p_data.get('issue_count', 0) + j_data.get('issue_count', 0)

            p_summary = p_data.get('summary') or {}
            j_summary = j_data.get('summary') or {}

            task.write({
                'prompt_text': prompt or task.prompt_text,
                'justification_text': justification or task.justification_text,
                'grammar_checked': True,
                'grammar_is_perfect': result['is_perfect'],
                'prompt_error_percentage': p_data.get('error_percentage', 0),
                'justification_error_percentage': j_data.get('error_percentage', 0),
                'prompt_issue_count': p_data.get('issue_count', 0),
                'justification_issue_count': j_data.get('issue_count', 0),
                'total_grammar_issues': total_issues,
                'prompt_corrected': p_data.get('corrected', ''),
                'justification_corrected': j_data.get('corrected', ''),
                'prompt_grammar_count': p_summary.get('grammar', 0),
                'prompt_misspelling_count': p_summary.get('misspelling', 0),
                'prompt_punctuation_count': p_summary.get('punctuation', 0),
                'prompt_clarity_count': p_summary.get('clarity', 0),
                'prompt_typography_count': p_summary.get('typography', 0),
                'prompt_capitalization_count': p_summary.get('capitalization', 0),
                'prompt_miscellaneous_count': p_summary.get('miscellaneous', 0),
                'justification_grammar_count': j_summary.get('grammar', 0),
                'justification_misspelling_count': j_summary.get('misspelling', 0),
                'justification_punctuation_count': j_summary.get('punctuation', 0),
                'justification_clarity_count': j_summary.get('clarity', 0),
                'justification_typography_count': j_summary.get('typography', 0),
                'justification_capitalization_count': j_summary.get('capitalization', 0),
                'justification_miscellaneous_count': j_summary.get('miscellaneous', 0),
            })

            if result['is_perfect']:
                return return_Response(message="No issues found.", status=200, data=result)

            return return_Response(
                message="Found %d issue(s)." % total_issues,
                status=200,
                data=result,
            )
        except Exception as e:
            _logger.error('Grammar check failed: %s', str(e))
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/tasks/<int:task_id>/rubric_ratings', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def get_task_rubric_ratings(self, task_id, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()
            task = TaskLog.browse(int(task_id))
            if not task.exists():
                return return_Response(message="Task not found", status=404)

            role = employee._get_task_forge_role() if hasattr(employee, '_get_task_forge_role') else 'tasker'
            if role not in ('admin', 'pl', 'qr', 'ql') and task.employee_id.id != employee.id:
                return return_Response(message="Not permitted", status=403)

            project = task.project_id
            rubric_categories = []
            if project:
                for cat in project.rubric_category_ids:
                    rubric_categories.append({
                        'id': cat.id,
                        'name': cat.name or '',
                        'description': cat.description or '',
                        'sequence': cat.sequence,
                        'options': [{
                            'id': opt.id,
                            'name': opt.name or '',
                            'value': opt.value,
                            'sequence': opt.sequence,
                        } for opt in cat.option_ids],
                        'dimensions': [{
                            'id': dim.id,
                            'name': dim.name or '',
                            'description': dim.description or '',
                            'is_required': dim.is_required,
                            'sequence': dim.sequence,
                            'options': [{
                                'id': o.id,
                                'name': o.name or '',
                                'value': o.value,
                            } for o in dim.option_ids],
                        } for dim in cat.dimension_ids],
                    })

            ratings = [{
                'id': r.id,
                'dimension_id': r.dimension_id.id,
                'dimension_name': r.dimension_name_snapshot or (r.dimension_id.name or ''),
                'option_id': r.option_id.id,
                'option_name': r.option_name_snapshot or (r.option_id.name or ''),
                'option_value': r.option_value_snapshot or r.option_id.value,
                'category_id': r.category_id.id,
            } for r in task.rubric_rating_ids]

            data = {
                'task_id': task.id,
                'project_id': project.id if project else False,
                'is_rubrics_required': bool(project and project.is_rubrics_required),
                'is_response_required': bool(project and project.is_response_required),
                'no_of_responses': project.no_of_responses if project else 0,
                'is_timer_enabled': bool(project and project.is_timer_enabled),
                'response_configs': [{
                    'id': cfg.id,
                    'label': cfg.label or '',
                    'sequence': cfg.sequence,
                } for cfg in project.response_config_ids.sorted('sequence')] if project else [],
                'responses': [{
                    'id': r.id,
                    'config_id': r.config_id.id if r.config_id else 0,
                    'label': r.label or '',
                    'sequence': r.sequence or 0,
                    'value': r.value or '',
                } for r in task.response_ids.sorted('sequence')],
                'response_completed': task.response_completed or False,
                'chain_of_thought': task.chain_of_thought or '',
                'rubric_completed': task.rubric_completed,
                'locked': task.state == 'completed',
                'rubric_categories': rubric_categories,
                'ratings': ratings,
                'is_justification_required': project.is_justification_required
            }
            return return_Response(message="Success", status=200, data={'data': data})
        except Exception as e:
            _logger.error('Get rubric ratings failed: %s', str(e))
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/tasks/review', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'task_id': {'type': 'int', 'required': True},
        'action': {'type': 'str', 'required': True},
    })
    def review_task(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            role = employee._get_task_forge_role()
            if role not in ('qr', 'ql', 'pl', 'admin'):
                return return_Response(message="Only QC/PL/Admin can review tasks", status=403)

            task_id = int(jdata.get('task_id'))
            action = jdata.get('action', '').strip().lower()

            if action not in ('approve', 'reject'):
                return return_Response(message="action must be 'approve' or 'reject'", status=400)

            TaskLog = request.env['task.forge.log'].sudo()
            task = TaskLog.browse(task_id)
            if not task.exists():
                return return_Response(message="Task not found", status=404)

            if task.state != 'completed':
                return return_Response(message="Only completed tasks can be reviewed", status=400)

            team_ids = employee._get_team_employee_ids()
            if role not in ('admin',) and task.employee_id.id not in team_ids:
                return return_Response(message="Access denied: task not in your team", status=403)

            vals = {
                'reviewed_by_id': employee.id,
                'review_date': datetime.now(),
            }

            if action == 'approve':
                vals['state'] = 'qc_approved'
                vals['rejection_reason'] = False
                task.write(vals)

                try:
                    request.env['kubera.notification'].sudo().create({
                        'title': 'Task Approved',
                        'message': f'Your task "{task.name}" has been approved by {employee.name}.',
                        'user_id': task.employee_id.user_id.id,
                        'priority': '1',
                        'res_model': 'task.forge.log',
                        'res_id': task.id,
                        'project_id': task.project_id.id if task.project_id else False,
                    })
                except Exception:
                    pass

                return return_Response(
                    message="Task approved",
                    status=200,
                    data={'data': self._format_task(task)}
                )
            else:
                rejection_reason = jdata.get('rejection_reason', '').strip()
                if not rejection_reason:
                    return return_Response(message="rejection_reason is required when rejecting", status=400)

                vals['state'] = 'qc_rejected'
                vals['rejection_reason'] = rejection_reason
                task.write(vals)

                try:
                    request.env['kubera.notification'].sudo().create({
                        'title': 'Task Rejected',
                        'message': f'Your task "{task.name}" has been rejected by {employee.name}. Reason: {rejection_reason[:200]}',
                        'user_id': task.employee_id.user_id.id,
                        'priority': '2',
                        'res_model': 'task.forge.log',
                        'res_id': task.id,
                        'project_id': task.project_id.id if task.project_id else False,
                    })
                except Exception:
                    pass

                return return_Response(
                    message="Task rejected",
                    status=200,
                    data={'data': self._format_task(task)}
                )
        except Exception as e:
            _logger.error('Review task failed: %s', str(e))
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/crons/trigger_all', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    # @validate_token
    def trigger_all_crons(self, **kwargs):
        try:
            request.update_env(user=2)
            results = []

            try:
                request.env['task.forge.log'].sudo()._cron_inactivity_check()
                results.append({'cron': 'Task Forge: Inactivity Check', 'status': 'success'})
            except Exception as e:
                results.append({'cron': 'Task Forge: Inactivity Check', 'status': 'failed', 'error': str(e)})

            try:
                request.env['hr.attendance'].sudo().create_attendance_record()
                results.append({'cron': 'Create Attendance Record (Auto 8:30h)', 'status': 'success'})
            except Exception as e:
                results.append({'cron': 'Create Attendance Record (Auto 8:30h)', 'status': 'failed', 'error': str(e)})

            try:
                request.env['project.blocker'].sudo()._cron_escalate_blockers()
                results.append({'cron': 'Project Blocker: Escalation Checker', 'status': 'success'})
            except Exception as e:
                results.append({'cron': 'Project Blocker: Escalation Checker', 'status': 'failed', 'error': str(e)})
            success_count = sum(1 for r in results if r['status'] == 'success')
            failed_count = sum(1 for r in results if r['status'] == 'failed')

            return return_Response(
                message=f"Crons executed: {success_count} success, {failed_count} failed",
                status=200,
                data={'data': results}
            )
        except Exception as e:
            _logger.error('Trigger all crons failed: %s', str(e))
            return return_Response(message=str(e), status=400)
