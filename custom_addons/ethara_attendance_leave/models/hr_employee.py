import logging
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)

# IST offset: UTC+5:30
IST_HOURS = 5
IST_MINUTES = 30


# Editable leave-balance grid: ethara_leave_code -> employee field name.
# Each field shows the employee's total *allocated* days for that leave type
# and is editable inline in the Leave Management list (sets/adjusts allocation).
ETHARA_BALANCE_FIELDS = {
    'sl': 'ethara_bal_sl',
    'cl': 'ethara_bal_cl',
    'el': 'ethara_bal_el',
    'lop': 'ethara_bal_lop',
    'marriage': 'ethara_bal_marriage',
    'maternity': 'ethara_bal_maternity',
    'bereavement': 'ethara_bal_bereavement',
    'comp_off': 'ethara_bal_comp_off',
    'wfh': 'ethara_bal_wfh',
    'menstrual': 'ethara_bal_menstrual',
    'restricted_holiday': 'ethara_bal_restricted',
}


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # --- Leave Management (HR) ---
    ethara_allocation_ids = fields.One2many(
        'hr.leave.allocation', 'employee_id',
        string='Leave Allocations',
        help='All leave allocations granted to this employee (drives the balance cards).')

    ethara_leave_type_count = fields.Integer(
        string='Leave Buckets',
        compute='_compute_ethara_leave_type_count',
        help='Number of distinct leave types this employee has a validated allocation for.')

    # --- Editable per-type balance columns (Leave Management list) ---
    ethara_bal_sl = fields.Float(
        string='Sick', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)
    ethara_bal_cl = fields.Float(
        string='Casual', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)
    ethara_bal_el = fields.Float(
        string='Earned', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)
    ethara_bal_lop = fields.Float(
        string='LOP', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)
    ethara_bal_marriage = fields.Float(
        string='Marriage', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)
    ethara_bal_maternity = fields.Float(
        string='Maternity', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)
    ethara_bal_bereavement = fields.Float(
        string='Bereavement', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)
    ethara_bal_comp_off = fields.Float(
        string='Comp-Off', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)
    ethara_bal_wfh = fields.Float(
        string='WFH', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)
    ethara_bal_menstrual = fields.Float(
        string='Menstrual', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)
    ethara_bal_restricted = fields.Float(
        string='Restricted Holiday', compute='_compute_ethara_balances',
        inverse='_inverse_ethara_balances', store=True)

    def _compute_ethara_leave_type_count(self):
        for emp in self:
            types = emp.ethara_allocation_ids.filtered(
                lambda a: a.state == 'validate').mapped('holiday_status_id')
            emp.ethara_leave_type_count = len(types)

    @api.model
    def _ethara_type_by_code(self):
        """{ethara_leave_code: hr.leave.type record} for the 11 Ethara types."""
        types = self.env['hr.leave.type'].sudo().search(
            [('ethara_leave_code', '!=', False)])
        return {t.ethara_leave_code: t for t in types}

    @api.depends('ethara_allocation_ids.number_of_days',
                 'ethara_allocation_ids.state',
                 'ethara_allocation_ids.holiday_status_id')
    def _compute_ethara_balances(self):
        for emp in self:
            totals = dict.fromkeys(ETHARA_BALANCE_FIELDS, 0.0)
            for alloc in emp.ethara_allocation_ids:
                if alloc.state != 'validate':
                    continue
                code = alloc.holiday_status_id.ethara_leave_code
                if code in totals:
                    totals[code] += alloc.number_of_days
            for code, fname in ETHARA_BALANCE_FIELDS.items():
                emp[fname] = totals[code]

    def _inverse_ethara_balances(self):
        """When HR edits a balance cell, reconcile the employee's allocations to it.

        Increase  -> create one validated allocation for the positive delta.
        Decrease  -> lower existing validated allocations (newest first), but never
                     below the days already taken for that type; regular allocations
                     must stay > 0, so a fully-cut one is refused out.
        After reconciling, the stored grid value is re-synced to the true allocation
        total (so a negative / below-taken input can't leave a wrong number on screen).
        """
        if self.env.context.get('ethara_sync'):
            return  # internal re-sync write below — don't recurse
        type_by_code = self._ethara_type_by_code()
        Allocation = self.env['hr.leave.allocation'].sudo()
        Leave = self.env['hr.leave'].sudo()
        today = fields.Date.today()
        year_start = today.replace(month=1, day=1)
        year_end = today.replace(month=12, day=31)
        for emp in self:
            for code, fname in ETHARA_BALANCE_FIELDS.items():
                leave_type = type_by_code.get(code)
                if not leave_type:
                    continue
                allocs = emp.ethara_allocation_ids.filtered(
                    lambda a: a.state == 'validate'
                    and a.holiday_status_id.id == leave_type.id)
                current = sum(allocs.mapped('number_of_days'))
                target = max(emp[fname] or 0.0, 0.0)
                delta = round(target - current, 2)
                if abs(delta) < 0.01:
                    continue
                if delta > 0:
                    alloc = Allocation.create({
                        'name': '%s (HR grid) %s' % (leave_type.name, today.year),
                        'holiday_status_id': leave_type.id,
                        'employee_id': emp.id,
                        'number_of_days': delta,
                        'date_from': year_start,
                        'date_to': year_end,
                        'allocation_type': 'regular',
                    })
                    self._ethara_validate_allocation(alloc)
                else:
                    # never reduce below days already taken for this type
                    taken = sum(Leave.search([
                        ('employee_id', '=', emp.id),
                        ('holiday_status_id', '=', leave_type.id),
                        ('state', '=', 'validate'),
                    ]).mapped('number_of_days'))
                    reduce_by = round(current - max(target, taken), 2)
                    if reduce_by > 0.01:
                        self._ethara_reduce_allocations(allocs, reduce_by)
            # re-sync the stored grid fields to the real allocation totals
            sync_vals = {}
            for code, fname in ETHARA_BALANCE_FIELDS.items():
                leave_type = type_by_code.get(code)
                if not leave_type:
                    continue
                sync_vals[fname] = sum(
                    a.number_of_days for a in emp.ethara_allocation_ids
                    if a.state == 'validate' and a.holiday_status_id.id == leave_type.id)
            emp.with_context(ethara_sync=True).write(sync_vals)

    @staticmethod
    def _ethara_reduce_allocations(allocations, amount):
        """Lower validated allocations (newest first) by `amount` days, never below
        what's already been taken on each one."""
        for alloc in allocations.sorted(key=lambda a: a.id, reverse=True):
            if amount <= 0.01:
                break
            floor = alloc.leaves_taken or 0.0
            reducible = alloc.number_of_days - floor
            if reducible <= 0:
                continue
            cut = min(reducible, amount)
            new_days = round(alloc.number_of_days - cut, 2)
            try:
                if new_days <= 0:
                    # regular allocations must be > 0 -> refuse it out of the balance
                    if hasattr(alloc, 'action_refuse'):
                        alloc.action_refuse()
                    else:
                        alloc.sudo().write({'state': 'refuse'})
                else:
                    alloc.sudo().write({'number_of_days': new_days})
                amount -= cut
            except Exception:  # noqa: BLE001 - skip an allocation we can't lower
                continue

    @staticmethod
    def _ethara_validate_allocation(allocation):
        """Push an allocation to state=validate so the balance counts."""
        try:
            if allocation.state == 'draft' and hasattr(allocation, 'action_confirm'):
                allocation.action_confirm()
            if allocation.state != 'validate':
                allocation.action_validate()
        except Exception:  # noqa: BLE001 - last resort: set state directly
            if allocation.state != 'validate':
                allocation.sudo().write({'state': 'validate'})

    def action_open_leave_balances(self):
        """Smart-button / row action: open this employee's leave buckets as cards."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('%s — Leave Balances') % self.name,
            'res_model': 'hr.leave.bucket',
            'view_mode': 'kanban,list',
            'domain': [('employee_id', '=', self.id)],
            'context': {'search_default_employee_id': self.id},
        }

    # --- Late Arrival Tracking ---
    late_arrival_count = fields.Integer(
        string='Late Arrivals (This Month)',
        compute='_compute_late_arrival_count',
        store=False,
        help='Number of late arrivals in the current month (after 10:30 AM IST).')

    total_late_penalty_deductions = fields.Float(
        string='Late Penalty Deductions (This Month)',
        compute='_compute_late_arrival_count',
        store=False,
        help='Total half-day deductions applied this month for late arrivals (from 4th late onwards).')

    # --- Weekly Deficit ---
    weekly_hour_deficit = fields.Float(
        string='Weekly Hour Deficit',
        compute='_compute_weekly_hour_deficit',
        store=False,
        help='Hours short of the 40-hour weekly minimum (8h/day × 5 days).')

    @api.depends_context('uid')
    def _compute_late_arrival_count(self):
        """Count late arrivals (check-in after 10:30 AM IST) for the current month."""
        from datetime import datetime, timedelta
        today = fields.Date.today()
        month_start = today.replace(day=1)

        has_attendance_status = 'attendance_status' in self.env['hr.attendance']._fields
        for emp in self:
            domain = [
                ('employee_id', '=', emp.id),
                ('check_in', '>=', datetime.combine(month_start, datetime.min.time())),
                ('check_in', '<=', datetime.combine(today, datetime.max.time())),
            ]
            if has_attendance_status:
                domain.append(('attendance_status', '=', 'present'))
            attendances = self.env['hr.attendance'].sudo().search(domain)
            late_count = 0
            for att in attendances:
                if att.check_in:
                    # Convert UTC to IST
                    ist_time = att.check_in + timedelta(hours=IST_HOURS, minutes=IST_MINUTES)
                    # Late = check-in after 10:30 AM IST
                    if ist_time.hour > 10 or (ist_time.hour == 10 and ist_time.minute > 30):
                        late_count += 1
            emp.late_arrival_count = late_count
            # First 3 are free, from 4th onwards: 0.5 day deduction each
            penalty_count = max(0, late_count - 3)
            emp.total_late_penalty_deductions = penalty_count * 0.5

    @api.depends_context('uid')
    def _compute_weekly_hour_deficit(self):
        """Compute the deficit between expected 40h/week and actual productive hours."""
        from datetime import datetime, timedelta
        today = fields.Date.today()
        # Start of current week (Monday)
        week_start = today - timedelta(days=today.weekday())

        has_attendance_status = 'attendance_status' in self.env['hr.attendance']._fields
        for emp in self:
            domain = [
                ('employee_id', '=', emp.id),
                ('check_in', '>=', datetime.combine(week_start, datetime.min.time())),
                ('check_in', '<=', datetime.combine(today, datetime.max.time())),
            ]
            if has_attendance_status:
                domain.append(('attendance_status', '=', 'present'))
            attendances = self.env['hr.attendance'].sudo().search(domain)
            total_productive = 0.0
            for att in attendances:
                if att.worked_hours:
                    # Subtract 1 hour break per day
                    productive = max(0.0, att.worked_hours - 1.0)
                    total_productive += productive

            # Days elapsed so far this week (Mon=0..Sun=6)
            days_elapsed = min(today.weekday() + 1, 5)  # cap at 5 working days
            expected_hours = days_elapsed * 8.0
            emp.weekly_hour_deficit = max(0.0, expected_hours - total_productive)

    # --- EL Encashment ---
    def compute_el_encashment(self):
        """
        Calculate EL encashment amount upon employee separation.
        Formula: (Basic Salary × min(EL Balance, 7)) / 22
        Returns dict: {employee_id: {'el_balance': float, 'encashment_amount': float}}
        """
        self.ensure_one()
        el_type = self.env.ref('ethara_attendance_leave.leave_type_el', raise_if_not_found=False)
        if not el_type:
            return {'el_balance': 0.0, 'encashment_amount': 0.0}

        # Get current EL allocation balance
        allocations = self.env['hr.leave.allocation'].sudo().search([
            ('employee_id', '=', self.id),
            ('holiday_status_id', '=', el_type.id),
            ('state', '=', 'validate'),
        ])
        el_balance = sum(a.number_of_days - a.leaves_taken for a in allocations)
        encashable_days = min(el_balance, 7.0)

        # Get basic salary from contract
        contract = self.env['hr.contract'].sudo().search([
            ('employee_id', '=', self.id),
            ('state', '=', 'open'),
        ], limit=1)
        basic_salary = contract.wage if contract else 0.0

        encashment_amount = (basic_salary * encashable_days) / 22.0 if basic_salary else 0.0

        return {
            'el_balance': el_balance,
            'encashable_days': encashable_days,
            'basic_salary': basic_salary,
            'encashment_amount': round(encashment_amount, 2),
        }

    # --- Monthly Late Penalty Processing ---
    @api.model
    def _cron_process_late_penalties(self):
        """
        Monthly cron: For each employee, if late_arrival_count > 3,
        deduct 0.5 days from CL (or SL as fallback) for each excess late arrival.
        """
        today = fields.Date.today()
        month_start = today.replace(day=1)
        employees = self.sudo().search([('active', '=', True)])

        cl_type = self.env.ref('ethara_attendance_leave.leave_type_cl', raise_if_not_found=False)
        sl_type = self.env.ref('ethara_attendance_leave.leave_type_sl', raise_if_not_found=False)

        for emp in employees:
            late_count = emp.late_arrival_count
            penalty_days = max(0, late_count - 3) * 0.5

            if penalty_days <= 0:
                continue

            # Try to deduct from CL first, then SL
            leave_type = cl_type or sl_type
            if not leave_type:
                _logger.warning('No CL/SL leave type found for late penalty deduction for %s', emp.name)
                continue

            try:
                self.env['hr.leave.allocation'].sudo().create({
                    'name': 'Late Arrival Penalty - %s' % today.strftime('%B %Y'),
                    'holiday_status_id': leave_type.id,
                    'number_of_days': -penalty_days,
                    'allocation_type': 'accrual',
                    'employee_id': emp.id,
                    'date_from': month_start,
                    'date_to': today,
                    'notes': 'Auto-deduction: %d late arrivals, %s half-day(s) deducted' % (late_count, penalty_days),
                })
                _logger.info('Late penalty: %s days deducted from %s for %s', penalty_days, leave_type.name, emp.name)
            except Exception as e:
                _logger.error('Failed to apply late penalty for %s: %s', emp.name, str(e))

    # --- Monthly Leave Accrual ---
    @api.model
    def _cron_monthly_leave_accrual(self):
        """
        Monthly cron (runs 1st of each month): Allocate 1 SL, 1 CL, 1 EL per active employee.
        """
        employees = self.sudo().search([('active', '=', True)])
        sl_type = self.env.ref('ethara_attendance_leave.leave_type_sl', raise_if_not_found=False)
        cl_type = self.env.ref('ethara_attendance_leave.leave_type_cl', raise_if_not_found=False)
        el_type = self.env.ref('ethara_attendance_leave.leave_type_el', raise_if_not_found=False)

        today = fields.Date.today()
        current_year = today.year
        year_end = fields.Date.from_string('%s-12-31' % current_year)

        for emp in employees:
            leave_types_to_allocate = []

            if sl_type:
                leave_types_to_allocate.append(sl_type)

            if cl_type:
                leave_types_to_allocate.append(cl_type)

            if el_type:
                leave_types_to_allocate.append(el_type)

            for lt in leave_types_to_allocate:
                try:
                    self.env['hr.leave.allocation'].sudo().create({
                        'name': '%s - %s %s' % (lt.name, today.strftime('%B'), current_year),
                        'holiday_status_id': lt.id,
                        'number_of_days': 1,
                        'employee_id': emp.id,
                        'date_from': today,
                        'date_to': year_end,
                    })
                except Exception as e:
                    _logger.error('Failed to allocate %s for %s: %s', lt.name, emp.name, str(e))

    # --- Yearly Lapse / Carry-Forward ---
    @api.model
    def _cron_yearly_leave_lapse(self):
        """
        Yearly cron (runs Jan 1): Lapse SL and CL balances. Cap EL carry-forward at 7 days.
        """
        employees = self.sudo().search([('active', '=', True)])
        sl_type = self.env.ref('ethara_attendance_leave.leave_type_sl', raise_if_not_found=False)
        cl_type = self.env.ref('ethara_attendance_leave.leave_type_cl', raise_if_not_found=False)
        el_type = self.env.ref('ethara_attendance_leave.leave_type_el', raise_if_not_found=False)

        previous_year = fields.Date.today().year - 1
        prev_year_end = fields.Date.from_string('%s-12-31' % previous_year)

        for emp in employees:
            # Lapse SL: expire all previous year allocations
            if sl_type:
                self._expire_allocations(emp, sl_type, prev_year_end)

            # Lapse CL: expire all previous year allocations
            if cl_type:
                self._expire_allocations(emp, cl_type, prev_year_end)

            # EL: carry forward max 7 days, expire the rest
            if el_type:
                self._cap_el_carry_forward(emp, el_type, max_days=7)

    def _expire_allocations(self, employee, leave_type, expiry_date):
        """Set expiration date on all allocations for given leave type to force lapse."""
        allocations = self.env['hr.leave.allocation'].sudo().search([
            ('employee_id', '=', employee.id),
            ('holiday_status_id', '=', leave_type.id),
            ('state', '=', 'validate'),
            ('date_to', '<=', expiry_date),
        ])
        for alloc in allocations:
            try:
                # Set the carried over days expiration so Odoo handles the lapse
                remaining = alloc.number_of_days - alloc.leaves_taken
                if remaining > 0:
                    alloc.write({
                        'expiring_carryover_days': remaining,
                        'carried_over_days_expiration_date': expiry_date,
                    })
            except Exception as e:
                _logger.error('Failed to expire allocation %s for %s: %s', alloc.id, employee.name, str(e))

    def _cap_el_carry_forward(self, employee, el_type, max_days=7):
        """
        For EL: calculate total remaining balance. If > max_days, expire the excess.
        """
        allocations = self.env['hr.leave.allocation'].sudo().search([
            ('employee_id', '=', employee.id),
            ('holiday_status_id', '=', el_type.id),
            ('state', '=', 'validate'),
        ])
        total_remaining = sum(a.number_of_days - a.leaves_taken for a in allocations)
        excess = total_remaining - max_days

        if excess > 0:
            # Create a negative allocation to cap the balance
            try:
                today = fields.Date.today()
                self.env['hr.leave.allocation'].sudo().create({
                    'name': 'EL Carry-Forward Cap - %s' % today.year,
                    'holiday_status_id': el_type.id,
                    'number_of_days': -excess,
                    'allocation_type': 'accrual',
                    'employee_id': employee.id,
                    'date_from': today,
                    'notes': 'Auto-lapse: EL balance exceeded %d-day carry-forward cap' % max_days,
                })
                _logger.info('EL cap: %s excess days lapsed for %s', excess, employee.name)
            except Exception as e:
                _logger.error('Failed to cap EL for %s: %s', employee.name, str(e))

    # --- Absence Alert ---
    @api.model
    def _cron_check_unauthorized_absence(self):
        """
        Daily cron: Check if any employee has >= 5 consecutive working days of
        unauthorized absence. If so, notify HR group.
        """
        from datetime import timedelta
        today = fields.Date.today()
        employees = self.sudo().search([('active', '=', True)])

        for emp in employees:
            consecutive_absent = 0
            check_date = today

            for _i in range(10):  # Check last 10 calendar days max
                # Skip weekends
                if check_date.weekday() >= 5:
                    check_date -= timedelta(days=1)
                    continue

                # Check for approved leave on this day
                approved_leave = self.env['hr.leave'].sudo().search([
                    ('employee_id', '=', emp.id),
                    ('state', '=', 'validate'),
                    ('request_date_from', '<=', check_date),
                    ('request_date_to', '>=', check_date),
                ], limit=1)

                if approved_leave:
                    break  # Has approved leave, not unauthorized

                # Check attendance
                att_domain = [
                    ('employee_id', '=', emp.id),
                    ('date', '=', check_date),
                ]
                if 'attendance_status' in self.env['hr.attendance']._fields:
                    att_domain.append(('attendance_status', '=', 'present'))
                attendance = self.env['hr.attendance'].sudo().search(att_domain, limit=1)

                if attendance:
                    break  # Was present
                else:
                    consecutive_absent += 1

                check_date -= timedelta(days=1)

            if consecutive_absent >= 5:
                # Send notification to HR
                try:
                    hr_group = self.env.ref('hr.group_hr_manager', raise_if_not_found=False)
                    if hr_group:
                        hr_users = hr_group.users
                        for hr_user in hr_users:
                            emp.message_post(
                                body='ALERT: %s has %d consecutive unauthorized absences. '
                                     'Please review for possible termination action.' % (emp.name, consecutive_absent),
                                partner_ids=hr_user.partner_id.ids,
                                subtype_xmlid='mail.mt_note',
                            )
                    _logger.warning('Unauthorized absence alert: %s - %d days', emp.name, consecutive_absent)
                except Exception as e:
                    _logger.error('Failed to send absence alert for %s: %s', emp.name, str(e))

    # --- Auto-allocate leaves on employee creation ---
    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        for emp in employees:
            emp._allocate_initial_leaves()
        return employees

    def _allocate_initial_leaves(self):
        """Allocate initial leave balance for a new employee based on join month."""
        self.ensure_one()
        today = fields.Date.today()
        current_year = today.year
        remaining_months = 12 - today.month + 1  # Include current month
        year_end = fields.Date.from_string('%s-12-31' % current_year)

        sl_type = self.env.ref('ethara_attendance_leave.leave_type_sl', raise_if_not_found=False)
        cl_type = self.env.ref('ethara_attendance_leave.leave_type_cl', raise_if_not_found=False)
        el_type = self.env.ref('ethara_attendance_leave.leave_type_el', raise_if_not_found=False)

        allocations = []

        if sl_type:
            allocations.append({
                'name': 'Sick Leave - Initial %s' % current_year,
                'holiday_status_id': sl_type.id,
                'number_of_days': remaining_months,
                'employee_id': self.id,
                'date_from': today,
                'date_to': year_end,
            })

        if cl_type:
            allocations.append({
                'name': 'Casual Leave - Initial %s' % current_year,
                'holiday_status_id': cl_type.id,
                'number_of_days': remaining_months,
                'employee_id': self.id,
                'date_from': today,
                'date_to': year_end,
            })

        if el_type:
            allocations.append({
                'name': 'Earned Leave - Initial %s' % current_year,
                'holiday_status_id': el_type.id,
                'number_of_days': remaining_months,
                'employee_id': self.id,
                'date_from': today,
                'date_to': year_end,
            })

        for alloc_vals in allocations:
            try:
                self.env['hr.leave.allocation'].sudo().create(alloc_vals)
            except Exception as e:
                _logger.error('Failed to create initial allocation for %s: %s', self.name, str(e))
