# -*- coding: utf-8 -*-
"""Argus REST API endpoints.

* GET  /api/v2/argus/tasks         — list / filter / paginate
* GET  /api/v2/argus/task/detail   — single record
* POST /api/v2/argus/tasks         — create (input/output URL + prompt)
* POST /api/v2/argus/tasks/update  — update (input/output URL + prompt)
"""

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_request,
    validate_token,
)


class ArgusTaskController(http.Controller):

    @http.route(
        '/api/v2/argus/tasks',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_tasks(self, **kwargs):
        try:
            ArgusTask = request.env['argus.task'].sudo()

            domain = []
            if kwargs.get('task_status'):
                domain.append(('task_status', '=', kwargs.get('task_status')))
            if kwargs.get('qc_status'):
                domain.append(('qc_status', '=', kwargs.get('qc_status')))
            if kwargs.get('final_decision'):
                domain.append(('final_decision', '=', kwargs.get('final_decision')))
            if kwargs.get('employee_id'):
                try:
                    domain.append(('employee_id', '=', int(kwargs.get('employee_id'))))
                except (TypeError, ValueError):
                    pass
            if kwargs.get('search'):
                term = kwargs.get('search')
                domain += [
                    '|', '|', '|',
                    ('name', 'ilike', term),
                    ('email', 'ilike', term),
                    ('input_video_url', 'ilike', term),
                    ('output_video_url', 'ilike', term),
                ]

            try:
                limit = int(kwargs.get('limit') or 0) or None
            except (TypeError, ValueError):
                limit = None
            try:
                offset = int(kwargs.get('offset') or 0)
            except (TypeError, ValueError):
                offset = 0

            tasks = ArgusTask.search(
                domain,
                limit=limit,
                offset=offset,
                order='timestamp desc, id desc',
            )
            total = ArgusTask.search_count(domain)
            data = [self._format_task_brief(t) for t in tasks]

            return return_Response(
                message="Argus task list",
                status=200,
                data={'data': data, 'total': total},
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route(
        '/api/v2/argus/task/detail',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    @validate_request({
        'task_id': {'type': 'str', 'required': True},
    })
    def task_detail(self, **kwargs):
        try:
            task_id = int(kwargs.get('task_id'))
            task = request.env['argus.task'].sudo().browse(task_id)
            if not task.exists():
                return return_Response(message="Argus task not found", status=404)

            return return_Response(
                message="Argus task detail",
                status=200,
                data={'data': self._format_task_detail(task)},
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route(
        '/api/v2/argus/tasks',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    @validate_request({
        'input_video_url': {'type': 'string', 'required': True},
        'output_video_url': {'type': 'string', 'required': True},
        'prompt': {'type': 'string', 'required': True},
        'email': {'type': 'email', 'required': False},
    })
    def create_task(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id

            vals = {
                'input_video_url': jdata['input_video_url'],
                'output_video_url': jdata['output_video_url'],
                'prompt': jdata['prompt'],
            }

            # Owner email is required by the model; derive from the
            # caller's employee profile when not supplied so API
            # callers don't have to pass it on every request.
            email = jdata.get('email')
            if not email and employee:
                emp_su = employee.sudo()
                email = emp_su.work_email or emp_su.private_email or user.login
            if not email:
                return return_Response(
                    message=(
                        "Owner email is required and could not be "
                        "derived from the current user."
                    ),
                    status=400,
                )
            vals['email'] = email

            try:
                task = request.env['argus.task'].sudo().create(vals)
            except (ValidationError, UserError) as exc:
                # Surface model-level validation as 400 instead of 500.
                return return_Response(message=str(exc), status=400)

            return return_Response(
                message="Argus task created",
                status=200,
                data={'data': self._format_task_detail(task)},
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route(
        '/api/v2/argus/tasks/update',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    @validate_request({
        'task_id': {'type': 'int', 'required': True},
        'input_video_url': {'type': 'string', 'required': False},
        'output_video_url': {'type': 'string', 'required': False},
        'prompt': {'type': 'string', 'required': False},
    })
    def update_task(self, jdata=None, **kwargs):
        try:
            task = request.env['argus.task'].sudo().browse(jdata['task_id'])
            if not task.exists():
                return return_Response(message="Argus task not found", status=404)

            vals = {}
            for field_name in ('input_video_url', 'output_video_url', 'prompt'):
                value = jdata.get(field_name)
                if value:
                    vals[field_name] = value

            if not vals:
                return return_Response(
                    message=(
                        "Nothing to update — provide at least one of "
                        "input_video_url, output_video_url, prompt."
                    ),
                    status=400,
                )

            try:
                task.write(vals)
            except (ValidationError, UserError) as exc:
                return return_Response(message=str(exc), status=400)

            return return_Response(
                message="Argus task updated",
                status=200,
                data={'data': self._format_task_detail(task)},
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _format_task_brief(self, task):
        return {
            'id': task.id,
            'name': task.name or '',
            'email': task.email or '',
            'input_video_url': task.input_video_url or '',
            'output_video_url': task.output_video_url or '',
            'prompt': task.prompt or '',
            'task_status': task.task_status or '',
            'qc_status': task.qc_status or '',
            'final_decision': task.final_decision or '',
            'employee_id': task.employee_id.id if task.employee_id else 0,
            'employee_name': task.employee_id.name if task.employee_id else '',
            'pl_user_id': task.pl_user_id.id if task.pl_user_id else 0,
            'pl_user_name': task.pl_user_id.name if task.pl_user_id else '',
            'ql_user_id': task.ql_user_id.id if task.ql_user_id else 0,
            'ql_user_name': task.ql_user_id.name if task.ql_user_id else '',
            'timestamp': task.timestamp.isoformat() if task.timestamp else '',
        }

    def _format_task_detail(self, task):
        data = self._format_task_brief(task)
        data.update({
            'input_shortcode': task.input_shortcode or '',
            'output_shortcode': task.output_shortcode or '',
            'input_video_embed_url': task.input_video_embed_url or '',
            'output_video_embed_url': task.output_video_embed_url or '',
            'ql_remarks': task.ql_remarks or '',
            'tasker_remarks': task.tasker_remarks or '',
            'prompt_grammar_score': task.prompt_grammar_score or 0,
            'prompt_grammar_level': task.prompt_grammar_level or '',
            'prompt_grammar_feedback': task.prompt_grammar_feedback or '',
            'prompt_grammar_checked_on': (
                task.prompt_grammar_checked_on.isoformat()
                if task.prompt_grammar_checked_on else ''
            ),
            'duplicate_count': task.duplicate_count or 0,
            'is_duplicate': bool(task.is_duplicate),
            'duplicate_task_ids': task.duplicate_task_ids.ids if task.duplicate_task_ids else [],
            'active': bool(task.active),
        })
        return data
