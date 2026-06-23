import base64
import json
import logging

from odoo import http
from odoo.http import request
from odoo.exceptions import ValidationError, UserError

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

# Only these API roles may manage leaves.
ALLOWED_ROLE_XMLIDS = (
    "api_auth_gateway.role_hr_technical",
    "api_auth_gateway.role_cto_technical",
)


def _read_params():
    """Merge query/form params with a JSON body (POST endpoints)."""
    params = dict(request.params or {})
    try:
        raw_body = request.httprequest.get_data(as_text=True) or ""
        if raw_body:
            body = json.loads(raw_body)
            if isinstance(body, dict):
                params.update(body)
    except (json.JSONDecodeError, ValueError):
        pass
    return params


def _allowed_role_ids(env):
    ids = []
    for xmlid in ALLOWED_ROLE_XMLIDS:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids


def _is_allowed(env):
    """Only HR and CTO roles may manage leaves."""
    role = env.user.user_role
    return bool(role) and role.id in _allowed_role_ids(env)


def _forbidden():
    return return_Response(
        message="Only HR and CTO roles can manage leaves.", status=403)


def _ethara_leave_types(env):
    """The Ethara leave catalog (types carrying an ethara_leave_code), ordered."""
    return env['hr.leave.type'].sudo().search(
        [('ethara_leave_code', '!=', False)], order='sequence, id')


def _serialize_leave_type(lt):
    return {
        'id': lt.id,
        'name': lt.name,
        'code': lt.ethara_leave_code,
        'default_annual_days': lt.default_annual_days,
        'requires_allocation': lt.requires_allocation,
        'allow_half_day': lt.allow_half_day,
        'request_unit': lt.request_unit,
        'color': lt.color,
    }


def _employee_buckets(env, employee, leave_types):
    """Per-type allocated/taken/remaining cards for one employee."""
    Allocation = env['hr.leave.allocation'].sudo()
    cards = []
    for lt in leave_types:
        allocs = Allocation.search([
            ('employee_id', '=', employee.id),
            ('holiday_status_id', '=', lt.id),
            ('state', '=', 'validate'),
        ])
        allocated = sum(allocs.mapped('number_of_days'))
        taken = sum(allocs.mapped('leaves_taken'))
        cards.append({
            'leave_type_id': lt.id,
            'leave_type': lt.name,
            'code': lt.ethara_leave_code,
            'allocated': allocated,
            'taken': taken,
            'remaining': allocated - taken,
            'color': lt.color,
        })
    return cards


def _resolve_leave_type(env, ref):
    """Resolve a leave type by id, ethara_leave_code or name."""
    LeaveType = env['hr.leave.type'].sudo()
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        lt = LeaveType.browse(int(ref))
        return lt if lt.exists() else LeaveType
    ref = (ref or '').strip()
    lt = LeaveType.search([('ethara_leave_code', '=', ref.lower())], limit=1)
    if not lt:
        lt = LeaveType.search([('name', '=ilike', ref)], limit=1)
    return lt


def _validate_allocation(allocation):
    try:
        if allocation.state == 'draft' and hasattr(allocation, 'action_confirm'):
            allocation.action_confirm()
        if allocation.state != 'validate':
            allocation.action_validate()
    except Exception:  # noqa: BLE001
        if allocation.state != 'validate':
            allocation.sudo().write({'state': 'validate'})


class LeaveManagementController(http.Controller):

    # ----------------------------------------------------------------- #
    #  Catalog                                                          #
    # ----------------------------------------------------------------- #
    @http.route('/api/v1/leave_mgmt/leave_types', methods=['GET'],
                type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def leave_types(self, **params):
        env = request.env
        if not _is_allowed(env):
            return _forbidden()
        types = [_serialize_leave_type(lt) for lt in _ethara_leave_types(env)]
        return return_Response(message="Success", data={'data': {'leave_types': types}})

    # ----------------------------------------------------------------- #
    #  Employee list + bucket summary                                   #
    # ----------------------------------------------------------------- #
    @http.route('/api/v1/leave_mgmt/employees', methods=['GET'],
                type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def employees(self, **params):
        env = request.env
        if not _is_allowed(env):
            return _forbidden()
        employees = env['hr.employee'].sudo().search(
            [('active', '=', True)], order='name')
        # Aggregate balances from the bucket SQL view in two grouped queries
        # (instead of one search per employee-per-type).
        Bucket = env['hr.leave.bucket'].sudo()
        totals = {
            emp.id: (alloc or 0.0, remain or 0.0)
            for emp, alloc, remain in Bucket._read_group(
                [('employee_id', 'in', employees.ids)],
                ['employee_id'], ['allocated:sum', 'remaining:sum'])
        }
        bucket_counts = {
            emp.id: count
            for emp, count in Bucket._read_group(
                [('employee_id', 'in', employees.ids), ('allocated', '>', 0)],
                ['employee_id'], ['__count'])
        }
        result = []
        for emp in employees:
            allocated, remaining = totals.get(emp.id, (0.0, 0.0))
            result.append({
                'id': emp.id,
                'name': emp.name,
                'email': emp.work_email or '',
                'department': emp.department_id.name or '',
                'job_title': emp.job_id.name or '',
                'user_id': emp.user_id.id or False,
                'total_allocated': allocated,
                'total_remaining': remaining,
                'bucket_count': bucket_counts.get(emp.id, 0),
            })
        return return_Response(message="Success", data={'data': {'employees': result}})

    # ----------------------------------------------------------------- #
    #  One employee's bucket cards                                      #
    # ----------------------------------------------------------------- #
    @http.route('/api/v1/leave_mgmt/employee/<int:employee_id>/buckets',
                methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def employee_buckets(self, employee_id, **params):
        env = request.env
        if not _is_allowed(env):
            return _forbidden()
        employee = env['hr.employee'].sudo().browse(employee_id)
        if not employee.exists():
            return return_Response(message="Employee not found.", status=404)
        cards = _employee_buckets(env, employee, _ethara_leave_types(env))
        return return_Response(message="Success", data={'data': {
            'employee': {'id': employee.id, 'name': employee.name,
                         'email': employee.work_email or ''},
            'buckets': cards,
        }})

    # ----------------------------------------------------------------- #
    #  Assign a leave type + days to an employee                        #
    # ----------------------------------------------------------------- #
    @http.route('/api/v1/leave_mgmt/allocate', methods=['POST'],
                type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def allocate(self, **kwargs):
        env = request.env
        if not _is_allowed(env):
            return _forbidden()
        params = _read_params()

        employee = env['hr.employee'].sudo().browse(int(params.get('employee_id') or 0))
        if not employee.exists():
            return return_Response(message="Invalid or missing employee_id.", status=400)

        leave_type = _resolve_leave_type(env, params.get('leave_type'))
        if not leave_type:
            return return_Response(message="Invalid or missing leave_type.", status=400)

        try:
            days = float(params.get('days'))
        except (TypeError, ValueError):
            return return_Response(message="'days' must be a number.", status=400)
        if days < 0:
            return return_Response(message="'days' cannot be negative.", status=400)

        date_from = params.get('date_from')
        date_to = params.get('date_to')

        vals = {
            'name': '%s (HR assign)' % leave_type.name,
            'holiday_status_id': leave_type.id,
            'employee_id': employee.id,
            'number_of_days': days,
            'allocation_type': 'regular',
        }
        if date_from:
            vals['date_from'] = date_from
        if date_to:
            vals['date_to'] = date_to
        try:
            # savepoint so a validation failure rolls back the partial create
            with env.cr.savepoint():
                allocation = env['hr.leave.allocation'].sudo().create(vals)
                _validate_allocation(allocation)
        except (ValidationError, UserError) as exc:
            return return_Response(message=exc.args[0] if exc.args else str(exc), status=400)

        return return_Response(message="Allocation created.", data={'data': {
            'allocation_id': allocation.id,
            'employee_id': employee.id,
            'leave_type': leave_type.name,
            'days': days,
            'state': allocation.state,
        }})

    # ----------------------------------------------------------------- #
    #  Bulk upload (.xlsx, base64)                                       #
    # ----------------------------------------------------------------- #
    @http.route('/api/v1/leave_mgmt/bulk_upload', methods=['POST'],
                type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def bulk_upload(self, **kwargs):
        env = request.env
        if not _is_allowed(env):
            return _forbidden()
        params = _read_params()
        file_b64 = params.get('file') or params.get('file_base64')
        if not file_b64:
            return return_Response(
                message="Missing 'file' (base64-encoded .xlsx).", status=400)
        try:
            base64.b64decode(file_b64)  # validate it decodes
        except Exception:  # noqa: BLE001
            return return_Response(message="'file' is not valid base64.", status=400)

        wizard = env['hr.leave.allocation.bulk.upload'].sudo().create({
            'upload_file': file_b64,
            'upload_filename': params.get('filename') or 'upload.xlsx',
        })
        try:
            wizard.action_import()
        except Exception as exc:  # noqa: BLE001
            return return_Response(message=str(exc), status=400)

        return return_Response(message="Success", data={'data': {
            'summary': wizard.result_summary or '',
        }})
