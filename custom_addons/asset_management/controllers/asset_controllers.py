import logging

from odoo import fields, http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request,
)

_logger = logging.getLogger(__name__)


def _manager_role_ids():
    """Role IDs allowed to approve / reject / fulfil / return."""
    env = request.env
    refs = [
        'api_auth_gateway.role_cto_technical',
        'api_auth_gateway.role_tpm_technical',
        'api_auth_gateway.role_pl_technical',
        'api_auth_gateway.role_pl_stem',
        'api_auth_gateway.role_pl_non_stem',
    ]
    ids = []
    for ref in refs:
        rec = env.ref(ref, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids


def _is_manager(user):
    role_id = user.user_role.id if user.user_role else False
    if role_id and role_id in _manager_role_ids():
        return True
    if user.has_group('asset_management.group_asset_manager'):
        return True
    return False


def _serialize_category(cat):
    return {
        'id': cat.id,
        'name': cat.name or '',
        'code': cat.code or '',
        'asset_type': cat.asset_type or '',
        'is_returnable': bool(cat.is_returnable),
        'default_duration_days': cat.default_duration_days or 0,
    }


def _serialize_asset(asset):
    return {
        'id': asset.id,
        'asset_code': asset.asset_code or '',
        'name': asset.display_name or '',
        'model': asset.model or '',
        'serial_number': asset.serial_number or '',
        'vendor': asset.vendor or '',
        'category_id': asset.category_id.id if asset.category_id else 0,
        'category': asset.category_id.name if asset.category_id else '',
        'asset_type': asset.asset_type or '',
        'state': asset.state or '',
        'current_employee_id': asset.current_employee_id.id if asset.current_employee_id else 0,
        'current_employee': asset.current_employee_id.name if asset.current_employee_id else '',
        'purchase_date': asset.purchase_date.isoformat() if asset.purchase_date else '',
        'warranty_expiry': asset.warranty_expiry.isoformat() if asset.warranty_expiry else '',
        'renewal_date': asset.renewal_date.isoformat() if asset.renewal_date else '',
        'value': asset.value or 0.0,
    }


def _serialize_request(req, with_employee_snapshot=False):
    data = {
        'id': req.id,
        'name': req.name or '',
        'employee_id': req.employee_id.id if req.employee_id else 0,
        'employee': req.employee_id.name if req.employee_id else '',
        'project_id': req.project_id.id if req.project_id else 0,
        'project': req.project_id.name if req.project_id else '',
        'category_id': req.category_id.id if req.category_id else 0,
        'category': req.category_id.name if req.category_id else '',
        'asset_type': req.asset_type or '',
        'preferred_model': req.preferred_model or '',
        'quantity': req.quantity or 0,
        'justification': req.justification or '',
        'replace_assignment_id': req.replace_assignment_id.id if req.replace_assignment_id else 0,
        'state': req.state or '',
        'requested_by_id': req.requested_by.id if req.requested_by else 0,
        'requested_by': req.requested_by.name if req.requested_by else '',
        'approved_by_id': req.approved_by.id if req.approved_by else 0,
        'approved_by': req.approved_by.name if req.approved_by else '',
        'approval_date': req.approval_date.isoformat() if req.approval_date else '',
        'rejection_reason': req.rejection_reason or '',
        'asset_id': req.asset_id.id if req.asset_id else 0,
        'asset_code': req.asset_id.asset_code if req.asset_id else '',
        'assignment_id': req.assignment_id.id if req.assignment_id else 0,
        'create_date': req.create_date.isoformat() if req.create_date else '',
    }
    if with_employee_snapshot and req.employee_id:
        data['employee_snapshot'] = _serialize_employee_profile(req.employee_id)
    return data


def _serialize_employee_profile(employee):
    """Return the full snapshot used by the request form: identity,
    department, active projects, currently held assets."""
    if not employee:
        return {}
    Assignment = employee.env['asset.assignment'].sudo()
    Project = employee.env['project.project'].sudo()

    active = Assignment.search([
        ('employee_id', '=', employee.id),
        ('actual_return_date', '=', False),
    ])

    project_domain = ['|', '|',
                      ('project_lead', 'in', employee.id),
                      ('project_qc_reviewer', 'in', employee.id),
                      ('project_tasker', 'in', employee.id)]
    if hasattr(Project, '_task_forge_live_domain'):
        try:
            project_domain = Project._task_forge_live_domain() + project_domain
        except Exception:  # noqa: BLE001
            pass
    projects = Project.search(project_domain)

    return {
        'id': employee.id,
        'name': employee.name or '',
        'work_email': employee.work_email or '',
        'work_phone': employee.work_phone or '',
        'job_title': employee.job_id.name if employee.job_id else '',
        'department_id': employee.department_id.id if employee.department_id else 0,
        'department': employee.department_id.name if employee.department_id else '',
        'manager_id': employee.parent_id.id if employee.parent_id else 0,
        'manager': employee.parent_id.name if employee.parent_id else '',
        'user_role': (employee.user_id.user_role.name
                      if employee.user_id and employee.user_id.user_role else ''),
        'active_projects': [
            {'id': p.id, 'name': p.name} for p in projects
        ],
        'current_assets': [
            {
                'assignment_id': a.id,
                'name': a.name,
                'asset_id': a.asset_id.id if a.asset_id else 0,
                'asset_code': a.asset_id.asset_code if a.asset_id else '',
                'asset_name': a.asset_id.display_name if a.asset_id else '',
                'category_id': a.asset_id.category_id.id if a.asset_id and a.asset_id.category_id else 0,
                'category': a.asset_id.category_id.name if a.asset_id and a.asset_id.category_id else '',
                'assigned_date': a.assigned_date.isoformat() if a.assigned_date else '',
                'expected_return_date': a.expected_return_date.isoformat() if a.expected_return_date else '',
                'duration_days': a.duration_days or 0,
            }
            for a in active
        ],
        'current_asset_count': len(active),
    }


def _serialize_assignment(a):
    return {
        'id': a.id,
        'name': a.name or '',
        'asset_id': a.asset_id.id if a.asset_id else 0,
        'asset_code': a.asset_id.asset_code if a.asset_id else '',
        'asset_name': a.asset_id.display_name if a.asset_id else '',
        'category_id': a.asset_id.category_id.id if a.asset_id and a.asset_id.category_id else 0,
        'category': a.asset_id.category_id.name if a.asset_id and a.asset_id.category_id else '',
        'employee_id': a.employee_id.id if a.employee_id else 0,
        'employee': a.employee_id.name if a.employee_id else '',
        'project_id': a.project_id.id if a.project_id else 0,
        'project': a.project_id.name if a.project_id else '',
        'request_id': a.request_id.id if a.request_id else 0,
        'assigned_date': a.assigned_date.isoformat() if a.assigned_date else '',
        'expected_return_date': a.expected_return_date.isoformat() if a.expected_return_date else '',
        'actual_return_date': a.actual_return_date.isoformat() if a.actual_return_date else '',
        'duration_days': a.duration_days or 0,
        'is_active': bool(a.is_active),
        'is_overdue': bool(a.is_overdue),
        'condition_in': a.condition_in or '',
        'condition_out': a.condition_out or '',
        'assigned_by': a.assigned_by.name if a.assigned_by else '',
    }


def _team_employee_ids(user):
    employee = user.employee_id
    if not employee:
        return []
    # Delegate to the existing Task Forge hierarchy if available
    if hasattr(employee, '_get_team_employee_ids'):
        try:
            return employee._get_team_employee_ids()
        except Exception:  # noqa: BLE001
            pass
    return [employee.id]


class AssetEmployeeProfileController(http.Controller):
    """Read-only endpoints used by the request form to auto-fill employee
    details (email, department, current projects, current assets)."""

    @http.route('/api/v2/taskforge/me/asset_profile', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def my_asset_profile(self, **kwargs):
        try:
            employee = request.env.user.employee_id
            if not employee:
                return return_Response(message='Employee profile not found', status=404)
            return return_Response(
                message='success', status=200,
                data={'data': _serialize_employee_profile(employee.sudo())},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/employees/<int:employee_id>/asset_profile',
                methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def employee_asset_profile(self, employee_id, **kwargs):
        try:
            user = request.env.user
            self_emp = user.employee_id
            target = request.env['hr.employee'].sudo().browse(employee_id)
            if not target.exists():
                return return_Response(message='Employee not found', status=404)
            # Self is always allowed; non-self requires manager role.
            if self_emp and target.id != self_emp.id and not _is_manager(user):
                return return_Response(
                    message='Permission denied: manager role required.', status=403,
                )
            return return_Response(
                message='success', status=200,
                data={'data': _serialize_employee_profile(target)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)


class AssetCategoryController(http.Controller):

    @http.route('/api/v2/taskforge/asset_categories', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def list_asset_categories(self, **kwargs):
        try:
            categories = request.env['asset.category'].sudo().search([('active', '=', True)])
            return return_Response(
                message='success', status=200,
                data={'data': [_serialize_category(c) for c in categories]},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)


class AssetController(http.Controller):

    @http.route('/api/v2/taskforge/assets', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def list_assets(self, **kwargs):
        try:
            user = request.env.user
            if not _is_manager(user):
                return return_Response(
                    message='Permission denied: manager role required.', status=403,
                )
            domain = []
            if kwargs.get('category_id'):
                domain.append(('category_id', '=', int(kwargs['category_id'])))
            if kwargs.get('state'):
                domain.append(('state', '=', kwargs['state']))
            assets = request.env['asset.asset'].sudo().search(domain)
            return return_Response(
                message='success', status=200,
                data={'data': [_serialize_asset(a) for a in assets], 'total': len(assets)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/assets/available', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def list_available_assets(self, **kwargs):
        try:
            domain = [('state', '=', 'available')]
            if kwargs.get('category_id'):
                domain.append(('category_id', '=', int(kwargs['category_id'])))
            assets = request.env['asset.asset'].sudo().search(domain)
            return return_Response(
                message='success', status=200,
                data={'data': [_serialize_asset(a) for a in assets], 'total': len(assets)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)


class AssetRequestController(http.Controller):

    @http.route('/api/v2/taskforge/asset_requests', methods=['POST'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'category_id': {'type': 'int', 'required': True},
        'employee_id': {'type': 'int'},
        'project_id': {'type': 'int'},
        'preferred_model': {'type': 'string'},
        'quantity': {'type': 'int'},
        'justification': {'type': 'string'},
        'replace_assignment_id': {'type': 'int'},
    })
    def create_asset_request(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message='Employee profile not found', status=404)

            target_emp_id = int(jdata.get('employee_id') or employee.id)
            if target_emp_id != employee.id and not _is_manager(user):
                return return_Response(
                    message='You can only raise a request for yourself.', status=403,
                )

            vals = {
                'employee_id': target_emp_id,
                'category_id': int(jdata['category_id']),
                'preferred_model': jdata.get('preferred_model') or '',
                'quantity': int(jdata.get('quantity') or 1),
                'justification': jdata.get('justification') or '',
            }
            if jdata.get('project_id'):
                vals['project_id'] = int(jdata['project_id'])
            if jdata.get('replace_assignment_id'):
                vals['replace_assignment_id'] = int(jdata['replace_assignment_id'])

            req = request.env['asset.request'].sudo().create(vals)
            return return_Response(
                message='Asset request created', status=200,
                data={'data': _serialize_request(req, with_employee_snapshot=True)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/asset_requests', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def list_asset_requests(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message='Employee profile not found', status=404)
            if _is_manager(user):
                team_ids = _team_employee_ids(user) or []
                domain = [('employee_id', 'in', team_ids)] if team_ids else []
            else:
                domain = [('employee_id', '=', employee.id)]
            if kwargs.get('state'):
                domain.append(('state', '=', kwargs['state']))
            if kwargs.get('category_id'):
                domain.append(('category_id', '=', int(kwargs['category_id'])))
            reqs = request.env['asset.request'].sudo().search(domain)
            return return_Response(
                message='success', status=200,
                data={'data': [_serialize_request(r) for r in reqs], 'total': len(reqs)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/asset_requests/<int:req_id>', methods=['GET'],
                type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def get_asset_request(self, req_id, **kwargs):
        try:
            req = request.env['asset.request'].sudo().browse(req_id)
            if not req.exists():
                return return_Response(message='Request not found', status=404)
            return return_Response(
                message='success', status=200,
                data={'data': _serialize_request(req, with_employee_snapshot=True)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/asset_requests/submit', methods=['POST'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({'request_id': {'type': 'int', 'required': True}})
    def submit_asset_request(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}
            req = request.env['asset.request'].sudo().browse(int(jdata['request_id']))
            if not req.exists():
                return return_Response(message='Request not found', status=404)
            req.action_submit()
            return return_Response(
                message='Request submitted', status=200,
                data={'data': _serialize_request(req)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/asset_requests/approve', methods=['POST'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({'request_id': {'type': 'int', 'required': True}})
    def approve_asset_request(self, **kwargs):
        try:
            user = request.env.user
            if not _is_manager(user):
                return return_Response(
                    message='Permission denied: manager role required.', status=403,
                )
            jdata = kwargs.get('jdata') or {}
            req = request.env['asset.request'].sudo().browse(int(jdata['request_id']))
            if not req.exists():
                return return_Response(message='Request not found', status=404)
            req.action_approve()
            return return_Response(
                message='Request approved', status=200,
                data={'data': _serialize_request(req)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/asset_requests/reject', methods=['POST'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'request_id': {'type': 'int', 'required': True},
        'rejection_reason': {'type': 'string', 'required': True},
    })
    def reject_asset_request(self, **kwargs):
        try:
            user = request.env.user
            if not _is_manager(user):
                return return_Response(
                    message='Permission denied: manager role required.', status=403,
                )
            jdata = kwargs.get('jdata') or {}
            req = request.env['asset.request'].sudo().browse(int(jdata['request_id']))
            if not req.exists():
                return return_Response(message='Request not found', status=404)
            req.action_reject(reason=jdata.get('rejection_reason'))
            return return_Response(
                message='Request rejected', status=200,
                data={'data': _serialize_request(req)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/asset_requests/fulfil', methods=['POST'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'request_id': {'type': 'int', 'required': True},
        'asset_id': {'type': 'int', 'required': True},
    })
    def fulfil_asset_request(self, **kwargs):
        try:
            user = request.env.user
            if not _is_manager(user):
                return return_Response(
                    message='Permission denied: manager role required.', status=403,
                )
            jdata = kwargs.get('jdata') or {}
            req = request.env['asset.request'].sudo().browse(int(jdata['request_id']))
            if not req.exists():
                return return_Response(message='Request not found', status=404)
            req.write({'asset_id': int(jdata['asset_id'])})
            req.action_fulfil()
            return return_Response(
                message='Request fulfilled', status=200,
                data={'data': _serialize_request(req)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)


class AssetAssignmentController(http.Controller):

    @http.route('/api/v2/taskforge/my_assets', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def my_assets(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message='Employee profile not found', status=404)
            assignments = request.env['asset.assignment'].sudo().search([
                ('employee_id', '=', employee.id),
                ('actual_return_date', '=', False),
            ])
            return return_Response(
                message='success', status=200,
                data={'data': [_serialize_assignment(a) for a in assignments],
                      'total': len(assignments)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/asset_assignments', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def list_assignments(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message='Employee profile not found', status=404)
            if _is_manager(user):
                team_ids = _team_employee_ids(user) or []
                domain = [('employee_id', 'in', team_ids)] if team_ids else []
            else:
                domain = [('employee_id', '=', employee.id)]
            if kwargs.get('employee_id'):
                domain.append(('employee_id', '=', int(kwargs['employee_id'])))
            if kwargs.get('is_active') in ('true', '1', 1, True):
                domain.append(('actual_return_date', '=', False))
            elif kwargs.get('is_active') in ('false', '0', 0, False):
                domain.append(('actual_return_date', '!=', False))
            assignments = request.env['asset.assignment'].sudo().search(domain)
            return return_Response(
                message='success', status=200,
                data={'data': [_serialize_assignment(a) for a in assignments],
                      'total': len(assignments)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/asset_assignments/history', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def assignment_history(self, **kwargs):
        try:
            domain = []
            if kwargs.get('asset_id'):
                domain.append(('asset_id', '=', int(kwargs['asset_id'])))
            if kwargs.get('employee_id'):
                domain.append(('employee_id', '=', int(kwargs['employee_id'])))
            if not domain:
                return return_Response(
                    message='Provide asset_id or employee_id', status=400,
                )
            assignments = request.env['asset.assignment'].sudo().search(domain)
            return return_Response(
                message='success', status=200,
                data={'data': [_serialize_assignment(a) for a in assignments],
                      'total': len(assignments)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/asset_assignments/return', methods=['POST'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'assignment_id': {'type': 'int', 'required': True},
        'condition_out': {'type': 'string'},
    })
    def return_assignment(self, **kwargs):
        try:
            user = request.env.user
            if not _is_manager(user):
                return return_Response(
                    message='Permission denied: manager role required.', status=403,
                )
            jdata = kwargs.get('jdata') or {}
            a = request.env['asset.assignment'].sudo().browse(int(jdata['assignment_id']))
            if not a.exists():
                return return_Response(message='Assignment not found', status=404)
            a.action_return(condition_out=jdata.get('condition_out'))
            return return_Response(
                message='Assignment closed', status=200,
                data={'data': _serialize_assignment(a)},
            )
        except Exception as e:  # noqa: BLE001
            return return_Response(message=str(e), status=400)
