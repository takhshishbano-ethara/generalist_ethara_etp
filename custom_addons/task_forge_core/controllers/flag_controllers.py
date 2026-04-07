from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
import json


class TaskForgeFlagController(http.Controller):

    @http.route('/api/v2/taskforge/flags', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_flags(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            Flag = request.env['task.forge.flag'].sudo()
            employee = user.employee_id
            role = employee._get_task_forge_role() if employee else 'admin'

            domain = []
            if role == 'pl':
                team_ids = employee._get_team_employee_ids()
                domain = [
                    '|',
                    ('related_employee_id', 'in', team_ids),
                    ('related_employee_id', '=', False),
                ]

            flags = Flag.search(domain, order='create_date desc', limit=200)
            data = [{
                'id': f.id,
                'type': f.flag_type,
                'message': f.name,
                'severity': f.severity,
                'related_employee_id': f.related_employee_id.id if f.related_employee_id else None,
                'related_employee_name': f.related_employee_name or '',
                'related_project_id': f.related_project_id.id if f.related_project_id else None,
                'related_project_name': f.related_project_name or '',
                'is_resolved': f.is_resolved,
                'resolved_at': f.resolved_at.isoformat() if f.resolved_at else None,
                'created_at': f.create_date.isoformat() if f.create_date else '',
            } for f in flags]

            return return_Response(message="Flags list", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/flags', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'type': {'type': 'string', 'required': True},
        'message': {'type': 'string', 'required': True},
        'severity': {'type': 'string', 'required': False},
        'related_employee_id': {'type': 'int', 'required': False},
        'related_project_id': {'type': 'int', 'required': False},
    })
    def create_flag(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            Flag = request.env['task.forge.flag'].sudo()
            vals = {
                'flag_type': jdata['type'],
                'name': jdata['message'],
                'severity': jdata.get('severity', 'medium'),
            }
            if jdata.get('related_employee_id'):
                vals['related_employee_id'] = jdata['related_employee_id']
            if jdata.get('related_project_id'):
                vals['related_project_id'] = jdata['related_project_id']

            flag = Flag.create(vals)
            return return_Response(
                message="Flag created",
                status=200,
                data={'data': {
                    'id': flag.id,
                    'type': flag.flag_type,
                    'message': flag.name,
                    'severity': flag.severity,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/flags/resolve', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'flag_id': {'type': 'int', 'required': True},
    })
    def resolve_flag(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            Flag = request.env['task.forge.flag'].sudo()
            flag = Flag.browse(jdata['flag_id'])
            if not flag.exists():
                return return_Response(message="Flag not found", status=404)

            flag.action_resolve()
            return return_Response(message="Flag resolved", status=200)
        except Exception as e:
            return return_Response(message=str(e), status=400)
