from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
import json


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
                    ('task_forge_status', '=', 'live'),
                ])
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
