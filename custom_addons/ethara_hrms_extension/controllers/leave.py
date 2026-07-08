import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)


GREYTHR_STATUS_MAP = {
    'draft': 'Draft',
    'pending': 'Pending',
    'approved': 'Approved',
    'refused': 'Rejected',
    'cancelled': 'Cancelled',
    'other': 'Other',
}

TASKFORGE_STATUS_TO_GREYTHR = {
    'Pending': 'pending',
    'Approved': 'approved',
    'Rejected': 'refused',
}


class EtharaGreythrLeaveController(http.Controller):

    @http.route('/api/v1/greythr/leaves', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def list_greythr_leaves(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            if hasattr(employee, '_get_team_employee_ids'):
                team_ids = employee._get_team_employee_ids()
            else:
                team_ids = [employee.id]

            LeaveRequest = request.env['greythr.leave.request'].sudo()
            domain = [('employee_id', 'in', team_ids)]

            status_param = kwargs.get('status')
            greythr_status = TASKFORGE_STATUS_TO_GREYTHR.get(status_param)
            if greythr_status:
                domain.append(('status', '=', greythr_status))

            role_id = kwargs.get('role_id')
            if role_id:
                try:
                    domain.append(('employee_id.user_id.user_role', '=', int(role_id)))
                except (TypeError, ValueError):
                    pass

            total_count = LeaveRequest.search_count(domain)

            page = int(kwargs['page']) if kwargs.get('page') else 1
            limit = int(kwargs['limit']) if kwargs.get('limit') else 10
            offset = (page - 1) * limit
            if not kwargs.get('page'):
                limit = total_count
                offset = 0

            leaves = LeaveRequest.search(
                domain, order='create_date desc', limit=limit, offset=offset
            )
            data = [self._format_greythr_leave(l) for l in leaves]

            return return_Response(
                message="Leaves list",
                status=200,
                data={'data': data, 'total_record_count': total_count},
            )
        except Exception as exc:
            _logger.exception('list_greythr_leaves failed')
            return return_Response(message=str(exc), status=400)

    def _format_greythr_leave(self, rec):
        emp = rec.employee_id
        user = emp.user_id if emp else False
        qr = emp.task_forge_qr_id if emp and 'task_forge_qr_id' in emp._fields else False
        pl = emp.task_forge_pl_id if emp and 'task_forge_pl_id' in emp._fields else False
        role = ''
        if user and 'user_role' in user._fields and user.user_role:
            role = user.user_role.name or ''

        leave_type = rec.holiday_status_id
        is_paid = False
        if leave_type and 'is_paid' in leave_type._fields:
            is_paid = bool(leave_type.is_paid)

        return {
            'id': rec.id or 0,
            'employee_id': emp.id if emp else 0,
            'employee_name': emp.name if emp else '',
            'role': role,
            'from_date': str(rec.from_date) if rec.from_date else '',
            'to_date': str(rec.to_date) if rec.to_date else '',
            'qc_id': qr.id if qr else 0,
            'qc_name': qr.name if qr else 0,
            'pl_id': pl.id if pl else 0,
            'pl_name': pl.name if pl and pl.name else '',
            'reason': rec.reason or rec.remarks or '',
            'status': GREYTHR_STATUS_MAP.get(rec.status, rec.status or ''),
            'is_paid': is_paid,
            'approved_by_name': '',
            'created_at': rec.create_date.isoformat() if rec.create_date else '',
        }
