from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
from odoo.exceptions import ValidationError
from datetime import datetime, timedelta, date as date_type
import json
from odoo import fields


class TaskForgeLeaveController(http.Controller):

    @validate_token
    @http.route('/api/v2/get_leave_types', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_leave_types(self, **kwargs):
        try:
            user = request.env.user
            LeaveType = request.env['hr.leave.type']
            if user.employee_id:
                LeaveType = LeaveType.with_context(employee_id=user.employee_id.id)
            leave_types = LeaveType.search([('active', '=', True)])
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
        'holiday_status_id': {'type': 'int', 'required': False},
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
            if not holiday_status_id:
                leave_type = request.env['hr.leave.type'].sudo().search([], limit=1)
                if not leave_type:
                    return return_Response(message="Configuration Error: No Time Off types found in Odoo.", status=400)
                holiday_status_id = leave_type.id

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

            # 4. Attempt Creation
            try:
                new_leave = Leave.create({
                    'employee_id': employee.id,
                    'holiday_status_id': holiday_status_id,
                    'request_date_from': from_date,
                    'request_date_to': to_date,
                    'name': reason,
                    'x_reason': reason,
                })

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

            if approver_role == 'hr':
                can_approve = True
            elif approver_employee.id == requestor_employee.id:
                can_approve = approver_role == 'admin'
            else:
                can_approve = requestor_employee.id in approver_employee._get_team_employee_ids()

            if not can_approve:
                role_labels = {'tasker': 'Tasker', 'qr': 'QC', 'ql': 'QC Lead', 'pl': 'PL', 'tpm': 'TPM', 'admin': 'CTO', 'hr': 'HR'}
                return return_Response(
                    message=f"A {role_labels.get(approver_role, approver_role)} cannot approve leave for a {role_labels.get(requestor_role, requestor_role)}. Only authorized roles in the hierarchy can approve.",
                    status=403
                )

            leave.action_approve()

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

            if approver_role == 'hr':
                can_reject = True
            elif approver_employee.id == requestor_employee.id:
                can_reject = approver_role == 'admin'
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
        return {
            'id': leave.id if leave.id else 0,
            'employee_id': leave.employee_id.id if leave.employee_id.id else 0,
            'employee_name': leave.employee_id.name if leave.employee_id.name else "",
            'role': leave.employee_id.user_id.user_role.name if leave.employee_id.user_id.user_role.name else "",
            'from_date': str(leave.date_from) if leave.date_from else '',
            'to_date': str(leave.date_to) if leave.date_to else '',
            'qc_id': leave.employee_id.task_forge_qr_id.id if leave.employee_id.task_forge_qr_id else 0,
            'qc_name': leave.employee_id.task_forge_qr_id.name if leave.employee_id.task_forge_qr_id else 0,
            'pl_id': leave.employee_id.task_forge_pl_id.id if leave.employee_id.task_forge_pl_id else 0,
            'pl_name': leave.employee_id.task_forge_pl_id.name if leave.employee_id.task_forge_pl_id and leave.employee_id.task_forge_pl_id.name else "",
            'reason': leave.x_reason or leave.name or '',
            'status': state_map.get(leave.state, leave.state),
            'is_paid': leave.is_paid,
            'approved_by_name': leave.first_approver_id.name if leave.first_approver_id else '',
            'created_at': leave.create_date.isoformat() if leave.create_date else '',
        }

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

