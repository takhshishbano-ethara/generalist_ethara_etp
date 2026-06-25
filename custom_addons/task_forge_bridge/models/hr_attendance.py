import logging
import datetime
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    # --- Custom Fields ---
    geo_coordinates = fields.Char(
        string='Geo Coordinates',
        help='Latitude,Longitude captured at punch-in',
    )
    geo_location = fields.Char(
        string='Location Name',
        help='Human-readable location name',
    )
    tasks_done = fields.Integer(
        string='Tasks Done',
        compute='_compute_tasks_done',
        store=False,
    )
    attendance_status = fields.Selection([
        ('present', 'Present'),
        ('absent', 'Absent'),
        ('on_leave', 'Leave')
    ], string="Status", default='present')

    @api.model_create_multi
    def create(self, values):
        for vals in values:
            employee_id = vals.get('employee_id')
            check_in = vals.get('check_in')
            if employee_id and check_in:
                check_in_date = fields.Datetime.to_datetime(check_in).date()
                existing_attendance = self.search([
                    ('employee_id', '=', employee_id),
                    ('check_in', '>=', datetime.datetime.combine(check_in_date, datetime.datetime.min.time())),
                    ('check_in', '<=', datetime.datetime.combine(check_in_date, datetime.datetime.max.time()))
                ], limit=1)
                if existing_attendance:
                    apply_vals = dict(vals)
                    incoming_check_in = fields.Datetime.to_datetime(check_in)
                    if existing_attendance.check_in and incoming_check_in >= existing_attendance.check_in:
                        apply_vals.pop('check_in', None)
                    incoming_check_out = apply_vals.get('check_out')
                    if incoming_check_out and existing_attendance.check_out:
                        existing_check_out = fields.Datetime.to_datetime(existing_attendance.check_out)
                        new_check_out = fields.Datetime.to_datetime(incoming_check_out)
                        if new_check_out < existing_check_out:
                            apply_vals.pop('check_out', None)
                    if apply_vals:
                        existing_attendance.write(apply_vals)
                    return existing_attendance
        return super(HrAttendance, self).create(values)

    # --- Compute Logic ---
    @api.depends('employee_id', 'check_in')
    def _compute_tasks_done(self):
        # Using get() to safely check if the module exists
        TaskLog = self.env.get('task.forge.log')
        for rec in self:
            if TaskLog and rec.employee_id and rec.check_in:
                # Compare only the date part of the check_in datetime
                date_val = rec.check_in.date()
                rec.tasks_done = TaskLog.sudo().search_count([
                    ('employee_id', '=', rec.employee_id.id),
                    ('date', '=', date_val),
                    ('state', '=', 'completed'),
                ])
            else:
                rec.tasks_done = 0

    # --- Automation Logic ---
    @api.model
    def create_attendance_record(self):
        """
        Cron job or Manual trigger to:
        1. Auto-close forgotten check-ins from previous days.
        2. Mark remaining employees as 'Absent' or 'Leave' for today.
        """
        now = fields.Datetime.now()
        today_date = fields.Date.today()
        # Define the start of today (00:00:00)
        today_start = datetime.datetime.combine(today_date, datetime.time.min)

        # 1. AUTO-CLOSE OLD RECORDS (Records with no checkout from yesterday or older)
        open_past_attendances = self.sudo().search([
            ('check_out', '=', False),
            ('check_in', '<', today_start)
        ])

        for att in open_past_attendances:
            try:
                # Default checkout logic: 8.5 hours after check-in
                calculated_checkout = att.check_in + datetime.timedelta(hours=8, minutes=30)

                # Safety check: Ensure auto-checkout doesn't overlap with a later check-in
                next_attendance = self.sudo().search([
                    ('employee_id', '=', att.employee_id.id),
                    ('check_in', '>', att.check_in),
                    ('id', '!=', att.id)
                ], order='check_in asc', limit=1)

                if next_attendance and calculated_checkout > next_attendance.check_in:
                    # Set checkout 1 minute before the next known check-in
                    calculated_checkout = next_attendance.check_in - datetime.timedelta(minutes=1)

                att.write({'check_out': calculated_checkout})
            except Exception as e:
                _logger.error("Auto-close failed for Attendance ID %s: %s", att.id, str(e))

        # 2. CREATE 'ABSENT' OR 'LEAVE' RECORDS
        # Get IDs of all employees who have already checked in today
        employees_with_today_attendance = self.sudo().search([
            ('check_in', '>=', today_start)
        ]).mapped('employee_id').ids

        # Find active employees who are NOT in the list above
        unmarked_employees = self.env['hr.employee'].sudo().search([
            ('id', 'not in', employees_with_today_attendance),
            ('active', '=', True)
        ])

        for emp in unmarked_employees:
            try:
                # Check for approved leaves (validate state) for today's date
                approved_leave = self.env['hr.leave'].sudo().search([
                    ('employee_id', '=', emp.id),
                    ('state', '=', 'validate'),
                    ('request_date_from', '<=', today_date),
                    ('request_date_to', '>=', today_date),
                ], limit=1)

                # Determine status based on leave presence
                current_status = 'on_leave' if approved_leave else 'absent'

                self.sudo().create({
                    'employee_id': emp.id,
                    'check_in': now,
                    'check_out': now,
                    'attendance_status': current_status,
                    'in_mode': 'manual'
                })
            except Exception as e:
                _logger.error("Failed to create daily status for %s: %s", emp.name, str(e))

    # --- Automation Logic ---
    @api.model
    def close_open_attendance_record(self):
        now = fields.Datetime.now()
        today_date = fields.Date.today()
        today_start = datetime.datetime.combine(today_date, datetime.time.min)
        open_past_attendances = self.sudo().search([
            ('check_out', '=', False),
            ('check_in', '<', today_start)
        ])
        for att in open_past_attendances:
            try:
                calculated_checkout = att.check_in + datetime.timedelta(hours=8, minutes=30)
                next_attendance = self.sudo().search([
                    ('employee_id', '=', att.employee_id.id),
                    ('check_in', '>', att.check_in),
                    ('id', '!=', att.id)
                ], order='check_in asc', limit=1)
                if next_attendance and calculated_checkout > next_attendance.check_in:
                    calculated_checkout = next_attendance.check_in - datetime.timedelta(minutes=1)
                att.write({'check_out': calculated_checkout})
            except Exception as e:
                _logger.error("Auto-close failed for Attendance ID %s: %s", att.id, str(e))
