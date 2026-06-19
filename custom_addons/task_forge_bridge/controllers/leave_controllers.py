from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
from odoo.exceptions import ValidationError
from datetime import datetime, timedelta, date as date_type
import base64
import json
import logging
import mimetypes
from odoo import fields

_logger = logging.getLogger(__name__)


class TaskForgeLeaveController(http.Controller):

    @validate_token
    @http.route('/api/v2/get_leave_types', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_leave_types(self, **kwargs):
        try:
            leave_types = request.env['hr.leave.type'].sudo().search([
                ('active', '=', True),
                ('ethara_leave_code', 'in', ['sl', 'cl', 'el', 'lop']),
            ])
            type_list = [{'id': lt.id, 'name': lt.name or ""} for lt in leave_types]
            return return_Response(message="Leave types fetched successfully", status=200, data={"record": type_list})
        except Exception as e:
            return return_Response(message="Failed to fetch leave types", status=400, errors=[str(e)])

    @http.route('/api/v2/taskforge/leaves', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'from_date': {'type': 'date', 'required': True},
        'to_date': {'type': 'date', 'required': True},
        'reason': {'type': 'string', 'required': True},
        # holiday_status_id intentionally left untyped: when the client uploads
        # an attachment, the request is multipart/form-data and every field
        # arrives as a string. The handler coerces it to int below.
        'holiday_status_id': {'required': False},
        # Half-day fields. Both are strings on the wire because multipart
        # form-data has no native bool. The handler interprets common
        # truthy spellings below.
        'is_half_day': {'required': False},
        'half_day_period': {'required': False},
    })
    def apply_leave(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            user = request.env.user
            employee = user.employee_id

            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            from_date = jdata.get('from_date')
            to_date = jdata.get('to_date')
            reason = jdata.get('reason', 'Applied via TaskForge')

            if not from_date or not to_date:
                return return_Response(message="Start and End dates are required", status=400)

            # SL and CL are explicitly same-day eligible: the employee may
            # have already clocked in before falling sick or hitting an
            # emergency. Skip the "you worked that day" guard for those
            # two codes; keep it for everything else (EL, LOP, …).
            ht_id = jdata.get('holiday_status_id')
            try:
                ht_id = int(ht_id) if ht_id not in (None, '', 0, '0') else 0
            except (TypeError, ValueError):
                ht_id = 0
            same_day_eligible = False
            if ht_id:
                ht_rec = request.env['hr.leave.type'].sudo().browse(ht_id)
                if ht_rec and getattr(ht_rec, 'ethara_leave_code', '') in ('sl', 'cl'):
                    same_day_eligible = True

            if not same_day_eligible:
                Attendance = request.env['hr.attendance'].sudo()
                # Odoo stores attendance in UTC; check if any record exists between from_date and to_date
                existing_attendance = Attendance.search([
                    ('employee_id', '=', employee.id),
                    ('check_in', '>=', f"{from_date} 00:00:00"),
                    ('check_in', '<=', f"{to_date} 23:59:59"),
                ], limit=1)

                if existing_attendance:
                    return return_Response(
                        message=f"Action Denied: You have already marked attendance on {existing_attendance.check_in.date()}. You cannot apply for leave on a day you worked.",
                        status=400
                    )
            # 2. Strict Manual Overlap Check
            # We check this FIRST to provide a friendly 400 error instead of a 422 crash
            Leave = request.env['hr.leave'].sudo()
            existing_overlap = Leave.search([
                ('employee_id', '=', employee.id),
                ('state', 'not in', ['refuse', 'cancel']),
                ('request_date_from', '<=', f"{to_date} 00:00:00"),
                ('request_date_to', '>=', f"{from_date} 23:59:00"),
            ], limit=1)

            if existing_overlap:
                return return_Response(
                    message=f"Overlap Error: You already have a leave request from {existing_overlap.request_date_from} to {existing_overlap.request_date_to}.",
                    status=400
                )

            start = datetime.strptime(from_date, '%Y-%m-%d').date() if isinstance(from_date, str) else from_date
            end = datetime.strptime(to_date, '%Y-%m-%d').date() if isinstance(to_date, str) else to_date

            weekend_dates = []
            current = start
            while current <= end:
                if current.weekday() in (5, 6):
                    weekend_dates.append(current.strftime('%Y-%m-%d'))
                current += timedelta(days=1)

            if weekend_dates:
                return return_Response(
                    message=f"Cannot apply leave on weekends (Saturday/Sunday): {', '.join(weekend_dates)}",
                    status=400
                )

            company = request.env.company
            calendar = employee.resource_calendar_id or company.resource_calendar_id
            public_holidays = request.env['resource.calendar.leaves'].sudo().search([
                ('resource_id', '=', False),
                ('date_from', '<=', f"{to_date} 23:59:59"),
                ('date_to', '>=', f"{from_date} 00:00:00"),
                ('calendar_id', 'in', [False, calendar.id if calendar else False]),
                ('company_id', 'in', [company.id, False]),
            ])

            if public_holidays:
                holiday_names = [ph.name or ph.date_from.strftime('%Y-%m-%d') for ph in public_holidays]
                return return_Response(
                    message=f"Cannot apply leave on public holidays: {', '.join(holiday_names)}",
                    status=400
                )

            # 3. Handle Leave Type (Holiday Status)
            holiday_status_id = jdata.get('holiday_status_id')
            if holiday_status_id in (None, '', 0, '0'):
                holiday_status_id = None
            if holiday_status_id is None:
                leave_type = request.env['hr.leave.type'].sudo().search([], limit=1)
                if not leave_type:
                    return return_Response(message="Configuration Error: No Time Off types found in Odoo.", status=400)
                holiday_status_id = leave_type.id
            else:
                try:
                    holiday_status_id = int(holiday_status_id)
                except (TypeError, ValueError):
                    return return_Response(message="holiday_status_id must be an integer.", status=400)

            leave_type_rec = request.env['hr.leave.type'].sudo().browse(holiday_status_id)

            if leave_type_rec and leave_type_rec.ethara_leave_code == 'el':
                advance_notice_days = leave_type_rec.advance_notice_days or 0
                if advance_notice_days and start:
                    from odoo.fields import Date as OdooDate
                    days_until = (start - OdooDate.today()).days
                    if days_until < advance_notice_days:
                        return return_Response(
                            message='Earned Leave requires at least %d days advance notice. '
                                    'You are requesting leave starting %s (%d days from today).' % (
                                        advance_notice_days, start, days_until),
                            status=400
                        )

            if leave_type_rec.requires_allocation:
                alloc_data = leave_type_rec.get_allocation_data(employee, start)
                emp_alloc = alloc_data.get(employee, [])
                max_leaves = 0
                virtual_remaining = 0
                for _name, data, *_rest in emp_alloc:
                    max_leaves += data.get('max_leaves', 0)
                    virtual_remaining += data.get('virtual_remaining_leaves', 0)

                if not max_leaves:
                    message = ('You do not have any allocation for %s. '
                               'Please request an allocation before submitting your leave.' % leave_type_rec.name)
                    message += self._lop_recommendation_suffix(employee, leave_type_rec, start)
                    return return_Response(message=message, status=400)

                requested_days = 0
                current = start
                while current <= end:
                    if current.weekday() not in (5, 6):
                        requested_days += 1
                    current += timedelta(days=1)

                if not leave_type_rec.allows_negative and requested_days > virtual_remaining:
                    message = ('Insufficient leave balance for %s. '
                               'Requested: %g day(s), Available: %g day(s).' % (
                                   leave_type_rec.name, requested_days, virtual_remaining))
                    message += self._lop_recommendation_suffix(employee, leave_type_rec, start)
                    return return_Response(message=message, status=400)

            # Half-day handling. The wire carries `is_half_day` (truthy
            # string) and `half_day_period` ('am' = Session 1 morning, 'pm'
            # = Session 2 afternoon). The CL-only restriction lives in the
            # model's @api.constrains, so we just stamp the request_unit_*
            # fields and let Odoo validate.
            is_half_day = self._is_truthy(jdata.get('is_half_day'))
            half_day_period = (jdata.get('half_day_period') or '').lower().strip()
            if is_half_day:
                if half_day_period not in ('am', 'pm'):
                    return return_Response(
                        message="half_day_period must be 'am' (Session 1) or 'pm' (Session 2) when is_half_day is true.",
                        status=400,
                    )
                # Half-day is always a single calendar day.
                to_date = from_date

            # 4. Attempt Creation
            try:
                create_vals = {
                    'employee_id': employee.id,
                    'holiday_status_id': holiday_status_id,
                    'request_date_from': from_date,
                    'request_date_to': to_date,
                    'name': reason,
                    'x_reason': reason,
                }
                if is_half_day:
                    create_vals.update({
                        'request_unit_half': True,
                        'request_date_from_period': half_day_period,
                    })
                new_leave = Leave.create(create_vals)

                # 5. Optional attachment (multipart/form-data field named "attachment")
                self._save_attachment(new_leave)

                return return_Response(
                    message="Leave request submitted successfully",
                    status=200,
                    data={'data': self._format_leave(new_leave)}
                )

            except Exception as odoo_internal_error:
                return return_Response(message=str(odoo_internal_error), status=400)

        except Exception as e:
            return return_Response(message="An unexpected error occurred", status=400, errors=[str(e)])

    def _lop_recommendation_suffix(self, employee, leave_type_rec, target_date):
        if not leave_type_rec or leave_type_rec.ethara_leave_code not in ('sl', 'cl', 'el'):
            return ''
        LeaveType = request.env['hr.leave.type'].sudo()
        paid_types = LeaveType.search([('ethara_leave_code', 'in', ('sl', 'cl', 'el'))])
        total_remaining = 0
        for lt in paid_types:
            alloc_data = lt.get_allocation_data(employee, target_date)
            for _name, data, *_rest in alloc_data.get(employee, []):
                total_remaining += data.get('virtual_remaining_leaves', 0)
        if total_remaining > 0:
            return ''
        lop_type = LeaveType.search([('ethara_leave_code', '=', 'lop')], limit=1)
        if not lop_type:
            return ''
        return (" All paid leave balances (SL, CL, EL) are exhausted. "
                "Please apply for '%s' (Loss of Pay) instead." % lop_type.name)

    @http.route('/api/v2/taskforge/leaves', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_leaves(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()
            Leave = request.env['hr.leave'].sudo()
            domain = [('employee_id', 'in', team_ids)]

            status_param = kwargs.get('status')
            if status_param == 'Pending':
                domain.append(('state', '=', 'confirm'))
            elif status_param == 'Approved':
                domain.append(('state', '=', 'validate'))
            elif status_param == 'Rejected':
                domain.append(('state', '=', 'refuse'))

            if kwargs.get('role_id'):
                domain.append(('employee_id.user_id.user_role', '=', int(kwargs.get('role_id'))))

            page = int(kwargs.get('page')) if kwargs.get('page') else 1
            limit = int(kwargs.get('limit')) if kwargs.get('limit') else 10
            offset = (page - 1) * limit
            total_count = request.env['hr.leave'].sudo().search_count(domain)
            if not kwargs.get('page'):
                limit = total_count
                offset = 0
            leaves = Leave.search(domain, order='create_date desc', limit=limit, offset=offset)
            data = [self._format_leave(l) for l in leaves]

            return return_Response(message="Leaves list", status=200, data={'data': data, 'total_record_count': total_count})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/leaves/approve', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'leave_id': {'type': 'int', 'required': True},
        'comment': {'type': 'string', 'required': False},
    })
    def approve_leave(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            approver_employee = user.employee_id
            if not approver_employee:
                return return_Response(message="Employee profile not found", status=404)

            approver_role = approver_employee._get_task_forge_role()

            if approver_role not in ('admin', 'tpm', 'pl', 'qr', 'ql', 'hr'):
                return return_Response(message="Insufficient permissions to approve leaves", status=403)

            Leave = request.env['hr.leave'].sudo()
            leave = Leave.browse(jdata['leave_id'])
            if not leave.exists():
                return return_Response(message="Leave not found", status=404)

            requestor_employee = leave.employee_id
            if not requestor_employee:
                return return_Response(message="Leave has no linked employee", status=400)

            requestor_role = requestor_employee._get_task_forge_role()

            # No one can approve their own leave — HR included, even though
            # HR has blanket approval rights for everyone else. See spec:
            # "hr can approve anyones leave except own".
            if approver_employee.id == requestor_employee.id:
                can_approve = False
            elif approver_role == 'hr':
                can_approve = True
            else:
                can_approve = requestor_employee.id in approver_employee._get_team_employee_ids()

            if not can_approve:
                role_labels = {'tasker': 'Tasker', 'qr': 'QC', 'ql': 'QC Lead', 'pl': 'PL', 'tpm': 'TPM', 'admin': 'CTO', 'hr': 'HR'}
                return return_Response(
                    message=f"A {role_labels.get(approver_role, approver_role)} cannot approve leave for a {role_labels.get(requestor_role, requestor_role)}. Only authorized roles in the hierarchy can approve.",
                    status=403
                )

            leave.action_approve()

            self._stamp_decision(leave, user, (jdata or {}).get('comment'))

            # Notify the employee
            request.env['kubera.notification'].sudo().create({
                'title': 'Leave Approved',
                'message': f'Your leave from {leave.date_from} to {leave.date_to} has been approved.',
                'user_id': leave.employee_id.user_id.id,
                'priority': '1',
                'res_model': 'hr.leave',
                'res_id': leave.id,
            })

            return return_Response(
                message="Leave approved",
                status=200,
                data={'data': self._format_leave(leave)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/leaves/reject', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'leave_id': {'type': 'int', 'required': True},
        'comment': {'type': 'string', 'required': False},
    })
    def reject_leave(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            approver_employee = user.employee_id
            if not approver_employee:
                return return_Response(message="Employee profile not found", status=404)

            approver_role = approver_employee._get_task_forge_role()

            if approver_role not in ('admin', 'tpm', 'pl', 'qr', 'ql', 'hr'):
                return return_Response(message="Insufficient permissions to reject leaves", status=403)

            Leave = request.env['hr.leave'].sudo()
            leave = Leave.browse(jdata['leave_id'])
            if not leave.exists():
                return return_Response(message="Leave not found", status=404)

            requestor_employee = leave.employee_id
            if not requestor_employee:
                return return_Response(message="Leave has no linked employee", status=400)

            requestor_role = requestor_employee._get_task_forge_role()

            # Symmetric with approve: no one can reject their own leave.
            if approver_employee.id == requestor_employee.id:
                can_reject = False
            elif approver_role == 'hr':
                can_reject = True
            else:
                can_reject = requestor_employee.id in approver_employee._get_team_employee_ids()

            if not can_reject:
                role_labels = {'tasker': 'Tasker', 'qr': 'QC', 'ql': 'QC Lead', 'pl': 'PL', 'tpm': 'TPM', 'admin': 'CTO', 'hr': 'HR'}
                return return_Response(
                    message=f"A {role_labels.get(approver_role, approver_role)} cannot reject leave for a {role_labels.get(requestor_role, requestor_role)}. Only authorized roles in the hierarchy can reject.",
                    status=403
                )

            leave.action_refuse()

            if not leave.first_approver_id:
                leave.sudo().write({'first_approver_id': approver_employee.id})

            self._stamp_decision(leave, user, (jdata or {}).get('comment'))

            request.env['kubera.notification'].sudo().create({
                'title': 'Leave Rejected',
                'message': f'Your leave from {leave.date_from} to {leave.date_to} has been rejected.',
                'user_id': leave.employee_id.user_id.id,
                'priority': '1',
                'res_model': 'hr.leave',
                'res_id': leave.id,
            })

            return return_Response(
                message="Leave rejected",
                status=200,
                data={'data': self._format_leave(leave)}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/leaves/hierarchy', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def leave_hierarchy(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Employee = request.env['hr.employee'].sudo()
            Leave = request.env['hr.leave'].sudo()
            today = datetime.now().date()
            role = employee._get_task_forge_role()

            if role == 'admin':
                # CTO: full tree — all PLs → their QRs → their Taskers
                pls = Employee.search([
                    ('user_id.user_role', 'in', [
                        request.env.ref('api_auth_gateway.role_pl_non_stem').id,
                        request.env.ref('api_auth_gateway.role_pl_technical').id,
                        request.env.ref('api_auth_gateway.role_pl_stem').id,
                    ])
                ])
                hierarchy = []
                for pl in pls:
                    hierarchy.append(self._build_pl_node(pl, Employee, Leave, today))
                return return_Response(message="Leave hierarchy", status=200, data={'data': hierarchy})

            elif role == 'pl':
                # PL: own subtree — self as PL → their QRs → their Taskers
                hierarchy = [self._build_pl_node(employee, Employee, Leave, today)]
                return return_Response(message="Leave hierarchy", status=200, data={'data': hierarchy})

            elif role in ('qr', 'ql'):
                # QR/QL: own taskers with leave data
                taskers = Employee.search([('task_forge_qr_id', '=', employee.id), ('task_forge_active', '=', True)])
                tasker_data = [self._build_tasker_node(t, Leave, today) for t in taskers]
                qr_node = {
                    'id': employee.id,
                    'name': employee.name,
                    'on_leave': self._is_on_leave(employee, Leave, today),
                    'pending_leaves': self._pending_leave_count(employee, Leave),
                    'taskers': tasker_data,
                }
                return return_Response(message="Leave hierarchy", status=200, data={'data': [qr_node]})

            else:
                # Tasker: own leave data only
                tasker_node = self._build_tasker_node(employee, Leave, today)
                return return_Response(message="Leave hierarchy", status=200, data={'data': [tasker_node]})

        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _is_on_leave(self, emp, Leave, today):
        """Check if employee is on approved leave today."""
        return bool(Leave.search_count([
            ('employee_id', '=', emp.id),
            ('date_from', '<=', today),
            ('date_to', '>=', today),
            ('state', '=', 'validate'),
        ]))

    def _pending_leave_count(self, emp, Leave):
        """Count pending leave requests for employee."""
        return Leave.search_count([
            ('employee_id', '=', emp.id),
            ('state', '=', 'confirm'),
        ])

    def _build_tasker_node(self, t, Leave, today):
        """Build leave data node for a tasker."""
        return {
            'id': t.id,
            'name': t.name,
            'on_leave': self._is_on_leave(t, Leave, today),
            'pending_leaves': self._pending_leave_count(t, Leave),
        }

    def _build_qr_node(self, qr, Employee, Leave, today):
        """Build QR node with tasker subtree."""
        taskers = Employee.search([('task_forge_qr_id', '=', qr.id), ('task_forge_active', '=', True)])
        return {
            'id': qr.id,
            'name': qr.name,
            'on_leave': self._is_on_leave(qr, Leave, today),
            'pending_leaves': self._pending_leave_count(qr, Leave),
            'taskers': [self._build_tasker_node(t, Leave, today) for t in taskers],
        }

    def _build_pl_node(self, pl, Employee, Leave, today):
        """Build PL node with full QR → Tasker subtree."""
        qrs = Employee.search([('task_forge_pl_id', '=', pl.id), ('task_forge_active', '=', True)])
        return {
            'id': pl.id,
            'name': pl.name,
            'on_leave': self._is_on_leave(pl, Leave, today),
            'qrs': [self._build_qr_node(qr, Employee, Leave, today) for qr in qrs],
        }

    def _format_leave(self, leave):
        state_map = {
            'draft': 'Draft',
            'confirm': 'Pending',
            'validate1': 'Pending',
            'validate': 'Approved',
            'refuse': 'Rejected',
        }
        attachment_url = ''
        attachment_filename = ''
        if leave.attachment_s3_key or leave.attachment:
            attachment_filename = leave.attachment_filename or 'attachment'
            # Prefer a presigned S3 URL when the file actually lives there:
            # the Flutter client can pull bytes straight from S3 with no
            # Odoo in the middle. URL is valid for 1 hour, which is much
            # longer than any drawer session.
            if leave.attachment_s3_key:
                try:
                    attachment_url = (
                        request.env['fenrir.s3.service']
                        .sudo()
                        .presigned_get_url(leave.attachment_s3_key, 3600)
                    )
                except Exception as exc:
                    _logger.warning(
                        "presigned URL failed for leave %s: %s — falling "
                        "back to Odoo proxy URL.", leave.id, exc,
                    )
                    attachment_url = (
                        '/v2/taskforge/leaves/%s/attachment' % leave.id
                    )
            else:
                # Legacy: file still in the Binary column → must go through
                # the Odoo endpoint (with bearer auth).
                attachment_url = '/v2/taskforge/leaves/%s/attachment' % leave.id

        # Use the Date fields, not the computed datetime ones. `date_from` is
        # stored in UTC and round-trips through the employee's timezone, which
        # was causing the response to read "2026-06-30 02:30:00" — neither
        # midnight nor what the user picked. `request_date_from` is a plain
        # Date and matches what the form submitted.
        from_date = ''
        to_date = ''
        if leave.request_date_from:
            from_date = leave.request_date_from.strftime('%Y-%m-%d')
        elif leave.date_from:
            from_date = leave.date_from.strftime('%Y-%m-%d')
        if leave.request_date_to:
            to_date = leave.request_date_to.strftime('%Y-%m-%d')
        elif leave.date_to:
            to_date = leave.date_to.strftime('%Y-%m-%d')

        leave_type_rec = leave.holiday_status_id
        leave_type = leave_type_rec.name or '' if leave_type_rec else ''
        leave_type_code = ''
        if leave_type_rec and hasattr(leave_type_rec, 'ethara_leave_code'):
            leave_type_code = leave_type_rec.ethara_leave_code or ''
        leave_type_id = leave_type_rec.id if leave_type_rec else 0

        # Decision audit. Prefer the explicit x_decision_* fields stamped by
        # the API; fall back to first_approver_id for legacy rows that pre-
        # date this column.
        decision_user = leave.x_decision_user_id
        decided_by_name = ''
        decided_by_role = ''
        if decision_user:
            decided_by_name = decision_user.name or ''
            user_role = getattr(decision_user, 'user_role', False)
            decided_by_role = user_role.name if user_role and user_role.name else ''
        elif leave.first_approver_id:
            decided_by_name = leave.first_approver_id.name or ''
            approver_user = leave.first_approver_id.user_id
            user_role = getattr(approver_user, 'user_role', False) if approver_user else False
            decided_by_role = user_role.name if user_role and user_role.name else ''
        decided_at = ''
        if leave.x_decision_date:
            decided_at = leave.x_decision_date.isoformat()
        decision_comment = leave.x_decision_comment or ''

        # approved_by_name now only carries the approver — leaving it empty
        # for rejected leaves so consumers can't confuse the rejector with
        # the approver (sheet row #16). New clients should read decided_*.
        approved_by_name = ''
        if leave.state == 'validate' and leave.first_approver_id:
            approved_by_name = leave.first_approver_id.name or ''

        return {
            'id': leave.id if leave.id else 0,
            'employee_id': leave.employee_id.id if leave.employee_id.id else 0,
            'employee_name': leave.employee_id.name if leave.employee_id.name else "",
            'role': leave.employee_id.user_id.user_role.name if leave.employee_id.user_id.user_role.name else "",
            'from_date': from_date,
            'to_date': to_date,
            'qc_id': leave.employee_id.task_forge_qr_id.id if leave.employee_id.task_forge_qr_id else 0,
            'qc_name': (
                leave.employee_id.task_forge_qr_id.name
                if leave.employee_id.task_forge_qr_id and leave.employee_id.task_forge_qr_id.name
                else ''
            ),
            'pl_id': leave.employee_id.task_forge_pl_id.id if leave.employee_id.task_forge_pl_id else 0,
            'pl_name': leave.employee_id.task_forge_pl_id.name if leave.employee_id.task_forge_pl_id and leave.employee_id.task_forge_pl_id.name else "",
            'reason': leave.x_reason or leave.name or '',
            'status': state_map.get(leave.state, leave.state),
            'is_paid': leave.is_paid,
            'approved_by_name': approved_by_name,
            'decided_by_name': decided_by_name,
            'decided_by_role': decided_by_role,
            'decided_at': decided_at,
            'decision_comment': decision_comment,
            'created_at': leave.create_date.isoformat() if leave.create_date else '',
            'attachment_url': attachment_url,
            'attachment_filename': attachment_filename,
            'leave_type': leave_type,
            'leave_type_code': leave_type_code,
            'holiday_status_id': leave_type_id,
            'total_days': leave.number_of_days or 0.0,
            'leave_balance': self._compute_balance(leave),
            'is_half_day': bool(leave.request_unit_half),
            # Empty string when the leave is a full-day request — keeps the
            # contract consistent (string, never null).
            'half_day_period': leave.request_date_from_period or '' if leave.request_unit_half else '',
        }

    @staticmethod
    def _is_truthy(value):
        """Coerce multipart-form / JSON values into a bool.

        multipart sends every field as a string, so `True`, `false`, `1`,
        `0`, and missing all need the same answer. Anything not explicitly
        truthy reads as False — including None.
        """
        if value is True:
            return True
        if value in (None, False, 0, '', '0', 'false', 'False', 'no', 'No'):
            return False
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'y', 'on')
        return bool(value)

    def _stamp_decision(self, leave, user, comment):
        """Record who took the final decision and what comment they typed.

        Called from both approve_leave and reject_leave so the audit trail
        is symmetric. `comment` is optional — when None/empty the column
        stays null so the UI can hide the "Decision comment" line.
        """
        if not leave or not user:
            return
        values = {
            'x_decision_user_id': user.id,
            'x_decision_date': fields.Datetime.now(),
        }
        text = (comment or '').strip()
        if text:
            values['x_decision_comment'] = text
        try:
            leave.sudo().write(values)
        except Exception:
            # The decision itself already succeeded - never bubble up an
            # audit-write failure to the caller.
            pass

    def _compute_balance(self, leave):
        """Return {applicable, entitled, taken, remaining, will_exceed} for the leave.

        applicable=False signals that this leave type does not use an annual
        allocation (e.g. LOP), in which case the numeric fields are zeros
        and consumers should hide the balance widget. The numbers come from
        the same `get_allocation_data` Odoo uses on the leave dashboards.
        `will_exceed` is true only when the leave is still pending and the
        currently-allocated balance would go negative if approved — which
        is what an approver actually needs to know.
        """
        empty = {
            'applicable': False,
            'entitled': 0.0,
            'taken': 0.0,
            'remaining': 0.0,
            'will_exceed': False,
        }
        leave_type = leave.holiday_status_id
        employee = leave.employee_id
        if not leave_type or not employee:
            return empty
        if not getattr(leave_type, 'requires_allocation', None):
            return empty
        try:
            ref_date = leave.request_date_from or fields.Date.today()
            alloc_data = leave_type.get_allocation_data(employee, ref_date)
        except Exception:
            return empty
        emp_alloc = alloc_data.get(employee) or alloc_data.get(employee.id) or []
        if not emp_alloc:
            return empty
        entitled = 0.0
        virtual_remaining = 0.0
        for entry in emp_alloc:
            # entry shape: (allocation_name, {max_leaves, virtual_remaining_leaves, ...})
            if not entry or len(entry) < 2:
                continue
            data = entry[1] or {}
            entitled += data.get('max_leaves', 0) or 0
            virtual_remaining += data.get('virtual_remaining_leaves', 0) or 0
        # `virtual_remaining_leaves` already deducts pending leaves, including
        # this one — so taken counts the in-flight request as well, which is
        # what an approver wants to see (entitled = taken + remaining always).
        taken = max(0.0, entitled - virtual_remaining)
        will_exceed = (
            leave.state in ('confirm', 'validate1') and virtual_remaining < 0
        )
        return {
            'applicable': True,
            'entitled': round(entitled, 2),
            'taken': round(taken, 2),
            'remaining': round(virtual_remaining, 2),
            'will_exceed': will_exceed,
        }

    def _save_attachment(self, leave):
        """Persist a multipart-uploaded file as the leave's attachment.

        Uploads the bytes to S3 via the shared `fenrir.s3.service` and
        records the resulting object key on `hr.leave.attachment_s3_key`.
        Falls back to the legacy Binary column when the S3 push fails so
        the user doesn't lose the file mid-flight; the download endpoint
        transparently reads from whichever column is populated.

        Silently no-ops when no file is present on the request, so the
        same endpoint keeps working for JSON-only callers.
        """
        try:
            files = request.httprequest.files
        except Exception:
            return
        upload = files.get('attachment') if files else None
        if not upload:
            return
        raw = upload.read()
        if not raw:
            return
        filename = upload.filename or 'attachment'
        mime = (
            upload.mimetype
            or mimetypes.guess_type(filename)[0]
            or 'application/octet-stream'
        )

        # Leave attachments live under their own top-level prefix
        # (`leaves/<leave_id>/<filename>`) — independent of fenrir's
        # configured folder so the S3 URL doesn't read like a fenrir
        # asset. The bucket / credentials still come from the shared
        # fenrir drive config.
        key = '/'.join(('leaves', str(leave.id), filename))

        try:
            request.env['fenrir.s3.service'].sudo().upload_bytes(key, raw, mime)
            leave.sudo().write({
                'attachment_s3_key': key,
                'attachment_filename': filename,
                # Clear any stale legacy bytes so we don't ship the same
                # file from two places.
                'attachment': False,
            })
            return
        except Exception as exc:
            _logger.warning(
                "S3 upload failed for leave %s (%s) — falling back to "
                "legacy ir.attachment storage.", leave.id, exc,
            )

        # Legacy fallback. Keeps the request alive when S3 is misconfigured.
        leave.sudo().write({
            'attachment': base64.b64encode(raw),
            'attachment_filename': filename,
            'attachment_s3_key': False,
        })

    @http.route('/api/v2/taskforge/leaves/<int:leave_id>/attachment',
                methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def get_leave_attachment(self, leave_id, **kwargs):
        """Stream a leave's attachment to authorized callers.

        Authorization mirrors the list/detail endpoints: the requester must
        be the leave owner, an approver in the same hierarchy, or HR/CTO.
        """
        try:
            user = request.env.user
            requester = user.employee_id
            if not requester:
                return return_Response(message="Employee profile not found", status=404)

            leave = request.env['hr.leave'].sudo().browse(leave_id)
            if not leave.exists() or not (leave.attachment_s3_key or leave.attachment):
                return return_Response(message="Attachment not found", status=404)

            requester_role = requester._get_task_forge_role()
            is_owner = leave.employee_id.id == requester.id
            is_privileged = requester_role in ('admin', 'hr', 'tpm')
            is_in_team = leave.employee_id.id in requester._get_team_employee_ids()
            if not (is_owner or is_privileged or is_in_team):
                return return_Response(message="Forbidden", status=403)

            # Prefer S3 storage; fall back to the legacy Binary column for
            # any rows that pre-date the S3 offload.
            payload = None
            if leave.attachment_s3_key:
                try:
                    payload = request.env['fenrir.s3.service'].sudo().download_bytes(
                        leave.attachment_s3_key
                    )
                except Exception as exc:
                    _logger.warning(
                        "S3 fetch failed for leave %s key=%s: %s",
                        leave.id, leave.attachment_s3_key, exc,
                    )
                    return return_Response(
                        message="Attachment fetch failed", status=502
                    )
            elif leave.attachment:
                try:
                    payload = base64.b64decode(leave.attachment)
                except Exception:
                    return return_Response(message="Attachment is corrupted", status=500)

            if payload is None:
                return return_Response(message="Attachment not found", status=404)

            filename = leave.attachment_filename or 'attachment'
            mimetype, _ = mimetypes.guess_type(filename)
            mimetype = mimetype or 'application/octet-stream'
            headers = [
                ('Content-Type', mimetype),
                ('Content-Length', str(len(payload))),
                ('Content-Disposition', 'inline; filename="%s"' % filename.replace('"', '')),
                ('Cache-Control', 'private, max-age=0, no-cache'),
            ]
            return request.make_response(payload, headers=headers)
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/today_leaves_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def today_leaves_list(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()
            Leave = request.env['hr.leave'].sudo()
            # current_projects = request.env['project.project'].sudo().search([])
            # if kwargs.get('project_id'):
            #     current_projects = request.env['project.project'].sudo().search([('id', '=', kwargs['project_id'])],
            #                                                                     limit=1)
            # employee_list = current_projects.mapped('project_lead') | current_projects.mapped('project_tasker') | current_projects.mapped('project_qc_reviewer')

            domain = [
                ('employee_id', 'in', team_ids),
                ('state', '=', 'validate'),
                ('date_from', '<=',
                 fields.Datetime.to_string(fields.Datetime.now().replace(hour=23, minute=59, second=59))),
                ('date_to', '>=', fields.Datetime.to_string(fields.Datetime.now().replace(hour=0, minute=0, second=0)))
            ]

            leaves = Leave.search(domain, order='create_date desc', limit=200)
            data = [self._format_leave(l) for l in leaves]

            return return_Response(message="Leaves list", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/check_i_am_on_leave', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def check_i_am_on_leave(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)
            Leave = request.env['hr.leave'].sudo()
            domain = [
                ('employee_id', '=', employee.id),
                ('state', '=', 'validate'),
                ('date_from', '<=',
                 fields.Datetime.to_string(fields.Datetime.now().replace(hour=23, minute=59, second=59))),
                ('date_to', '>=', fields.Datetime.to_string(fields.Datetime.now().replace(hour=0, minute=0, second=0)))
            ]
            leaves = Leave.search(domain, order='create_date desc', limit=1)
            return return_Response(message="Leaves list", status=200, data={'data': self._format_leave(leaves)})
        except Exception as e:
            return return_Response(message=str(e), status=400)

