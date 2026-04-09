from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
from datetime import datetime
import json


class AllocationController(http.Controller):

    @http.route('/api/v1/allocation/request', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'project_id': {'type': 'int', 'required': True},
        'role_id': {'type': 'int', 'required': True},
        'quantity': {'type': 'int', 'required': True},
        'justification': {'type': 'string', 'required': True},
    })
    def create_allocation_request(self, jdata=None, **kwargs):
        """Create a new allocation request"""
        try:
            user = request.env.user

            AllocationRequest = request.env['employee.allocation.request'].sudo()
            
            vals = {
                'project_id': jdata['project_id'],
                'quantity': jdata['quantity'],
                'justification': jdata['justification'],
                'requested_by': user.id,
                'state': 'submitted',
            }
            
            if jdata.get('role_id'):
                vals['role_id'] = jdata['role_id']
            
            allocation_request = AllocationRequest.create(vals)

            return return_Response(
                message="Allocation request created",
                status=200,
                data={'data': {
                    'id': allocation_request.id,
                    # 'name': allocation_request.name,
                    'project_id': allocation_request.project_id.id,
                    'project_name': allocation_request.project_id.name,
                    'quantity': allocation_request.quantity,
                    'state': allocation_request.state,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v1/allocation/request/<int:request_id>/submit', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def submit_allocation_request(self, request_id, **kwargs):
        """Submit an allocation request for approval"""
        try:
            AllocationRequest = request.env['employee.allocation.request'].sudo()
            allocation_request = AllocationRequest.browse(request_id)

            if not allocation_request.exists():
                return return_Response(message="Allocation request not found", status=404)

            if allocation_request.state != 'draft':
                return return_Response(
                    message=f"Cannot submit. Current state: {allocation_request.state}",
                    status=400
                )

            allocation_request.action_submit()

            return return_Response(
                message="Allocation request submitted",
                status=200,
                data={'data': {
                    'id': allocation_request.id,
                    'name': allocation_request.name,
                    'state': allocation_request.state,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v1/allocation/request/approve', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'assign_employees': {'type': 'list', 'required': True},
        'notes': {'type': 'string', 'required': True},
        'request_id': {"type": "int", "required": True},
    })
    def approve_allocation_request(self, **kwargs):
        """Approve an allocation request"""
        try:
            jdata = kwargs.get('jdata')
            user = request.env.user

            if user.user_role.id != request.env.ref('api_auth_gateway.role_cto_technical').id:
                return return_Response(message="Insufficient permissions to approve", status=403)

            AllocationRequest = request.env['employee.allocation.request'].sudo()
            allocation_request = AllocationRequest.browse(jdata.get('request_id'))

            if not allocation_request.exists():
                return return_Response(message="Allocation request not found", status=404)

            if allocation_request.state != 'submitted':
                return return_Response(
                    message=f"Cannot approve. Current state: {allocation_request.state}",
                    status=400
                )

            vals = {
                'state': 'approved',
                'approval_date': datetime.now(),
                'approved_by': user.id,
                'assign_employees': [(6, 0, jdata.get('assign_employees'))]
            }
            
            if jdata.get('notes'):
                vals['notes'] = jdata['notes']

            allocation_request.write(vals)
            if allocation_request.project_id:
                if allocation_request.role_id.id in [request.env.ref('api_auth_gateway.role_pl_technical').id,
                                                     request.env.ref('api_auth_gateway.role_pl_stem').id,
                                                     request.env.ref('api_auth_gateway.role_pl_non_stem').id]:
                    total_emp = allocation_request.project_id.project_lead.ids
                    total_emp.expend(jdata.get('assign_employees'))
                    allocation_request.project_id.sudo().project_lead = [(6, 0, total_emp)]
                if allocation_request.role_id.id in [request.env.ref('api_auth_gateway.role_qc_technical').id,
                                                     request.env.ref('api_auth_gateway.role_qc_stem').id,
                                                     request.env.ref('api_auth_gateway.role_qc_non_stem').id]:
                    total_emp = allocation_request.project_id.project_qc_reviewer.ids
                    total_emp.expend(jdata.get('assign_employees'))
                    allocation_request.project_id.sudo().project_qc_reviewer = [(6, 0, total_emp)]
                if allocation_request.role_id.id in [request.env.ref('api_auth_gateway.role_tasker_technical').id,
                                                     request.env.ref('api_auth_gateway.role_tasker_stem').id,
                                                     request.env.ref('api_auth_gateway.role_tasker_non_stem').id]:
                    total_emp = allocation_request.project_id.project_tasker.ids
                    total_emp.expend(jdata.get('assign_employees'))
                    allocation_request.project_id.sudo().project_tasker = [(6, 0, total_emp)]

            return return_Response(
                message="Allocation request approved",
                status=200,
                data={'data': {
                    'id': allocation_request.id,
                    'name': allocation_request.name,
                    'state': allocation_request.state,
                    'approved_by': user.name,
                    'approval_date': allocation_request.approval_date.isoformat() if allocation_request.approval_date else None,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v1/allocation/request/reject', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'request_id': {'type': 'int', 'required': True},
        'notes': {'type': 'string', 'required': False},
    })
    def reject_allocation_request(self, **kwargs):
        """Reject an allocation request"""
        try:
            jdata = kwargs.get('jdata')
            user = request.env.user
            if user.user_role.id != request.env.ref('api_auth_gateway.role_cto_technical').id:
                return return_Response(message="Insufficient permissions to approve", status=403)

            AllocationRequest = request.env['employee.allocation.request'].sudo()
            allocation_request = AllocationRequest.browse(jdata.get('request_id'))

            if not allocation_request.exists():
                return return_Response(message="Allocation request not found", status=404)

            if allocation_request.state != 'submitted':
                return return_Response(
                    message=f"Cannot reject. Current state: {allocation_request.state}",
                    status=400
                )

            vals = {
                'state': 'rejected',
            }
            
            if jdata.get('notes'):
                vals['notes'] = jdata['notes']

            allocation_request.write(vals)

            return return_Response(
                message="Allocation request rejected",
                status=200,
                data={'data': {
                    'id': allocation_request.id,
                    'name': allocation_request.name,
                    'state': allocation_request.state,
                    'notes': allocation_request.notes,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v1/allocation/request', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'request_id': {'type': 'str', 'required': True}
    })
    def get_allocation_request(self, **kwargs):
        """Get allocation request details"""
        try:
            jdata = kwargs.get('jdata')
            user = request.env.user
            if user.user_role.id != request.env.ref('api_auth_gateway.role_cto_technical').id:
                return return_Response(message="Insufficient permissions to approve", status=403)

            AllocationRequest = request.env['employee.allocation.request'].sudo()
            allocation_request = AllocationRequest.browse(int(jdata.get('request_id')))

            if not allocation_request.exists():
                return return_Response(message="Allocation request not found", status=404)

            return return_Response(
                message="Allocation request details",
                status=200,
                data={'data': {
                    'id': allocation_request.id,
                    'name': allocation_request.name,
                    'project_id': allocation_request.project_id.id,
                    'project_name': allocation_request.project_id.name,
                    'role_id': allocation_request.role_id.id if allocation_request.role_id else None,
                    'role_name': allocation_request.role_id.name if allocation_request.role_id else None,
                    'quantity': allocation_request.quantity,
                    'justification': allocation_request.justification,
                    'state': allocation_request.state,
                    'requested_by': allocation_request.requested_by.name if allocation_request.requested_by else None,
                    'approved_by': allocation_request.approved_by.name if allocation_request.approved_by else None,
                    'approval_date': allocation_request.approval_date.isoformat() if allocation_request.approval_date else None,
                    'notes': allocation_request.notes,
                    'created_at': allocation_request.create_date.isoformat() if allocation_request.create_date else None,
                    'assign_employees': [{'id': i.id, 'name': i.name} for i in allocation_request.assign_employees]
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v1/allocation/requests_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_allocation_requests(self, **kwargs):
        """List allocation requests with filters"""
        try:
            AllocationRequest = request.env['employee.allocation.request'].sudo()

            domain = []
            if kwargs.get('state'):
                domain.append(('state', '=', kwargs['state']))
            if kwargs.get('project_id'):
                domain.append(('project_id', '=', int(kwargs['project_id'])))

            limit = int(kwargs.get('limit', 100))
            requests = AllocationRequest.search(domain, limit=limit)

            data = [{
                'id': req.id,
                'name': req.name,
                'project_name': req.project_id.name if req.project_id else "",
                'role_name': req.role_id.name if req.role_id else "",
                'quantity': req.quantity,
                'state': req.state,
                'requested_by': req.requested_by.name if req.requested_by else "",
                'created_at': req.create_date.isoformat() if req.create_date else "",
                'assign_employees': [{'id': i.id, 'name': i.name} for i in req.assign_employees]
            } for req in requests]

            return return_Response(
                message=f"{len(data)} allocation requests found",
                status=200,
                data={'data': data}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v1/allocation/request/reset', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'request_id': {'type': 'str', 'required': True}
    })
    def reset_allocation_request(self, **kwargs):
        """Reset allocation request to draft"""
        try:
            AllocationRequest = request.env['employee.allocation.request'].sudo()
            allocation_request = AllocationRequest.browse(kwargs.get('request_id'))

            if not allocation_request.exists():
                return return_Response(message="Allocation request not found", status=404)

            if allocation_request.state not in ['rejected', 'approved']:
                return return_Response(
                    message=f"Cannot reset. Current state: {allocation_request.state}",
                    status=400
                )

            allocation_request.action_reset_draft()

            return return_Response(
                message="Allocation request reset to draft",
                status=200,
                data={'data': {
                    'id': allocation_request.id,
                    'name': allocation_request.name,
                    'state': allocation_request.state,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)
