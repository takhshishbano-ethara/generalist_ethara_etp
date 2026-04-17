import logging
from datetime import datetime, timedelta, time

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

# IST offset constants
IST_HOURS = 5
IST_MINUTES = 30

# Work schedule times in IST
FLEXI_START = time(9, 30)   # 9:30 AM IST — earliest allowed check-in
STANDARD_START = time(10, 0)  # 10:00 AM IST — standard start
GRACE_END = time(10, 30)    # 10:30 AM IST — max grace period
STANDARD_END = time(19, 0)  # 7:00 PM IST — standard end
MIN_PRODUCTIVE_HOURS = 8.0  # Excluding 1-hour break
BREAK_HOURS = 1.0


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    # --- Late Arrival Flag ---
    is_late = fields.Boolean(
        string='Late Arrival',
        compute='_compute_is_late',
        store=True,
        help='Checked if employee checked in after 10:30 AM IST (grace period).')

    check_in_ist_time = fields.Char(
        string='Check-In (IST)',
        compute='_compute_ist_times',
        store=False,
        help='Check-in time converted to IST for display.')

    check_out_ist_time = fields.Char(
        string='Check-Out (IST)',
        compute='_compute_ist_times',
        store=False)

    # --- Productive Hours ---
    productive_hours = fields.Float(
        string='Productive Hours',
        compute='_compute_productive_hours',
        store=True,
        help='Worked hours minus 1-hour break. Minimum expected: 8 hours.')

    is_productive_deficit = fields.Boolean(
        string='Productivity Deficit',
        compute='_compute_productive_hours',
        store=True,
        help='Flagged if productive hours < 8 for the day.')

    # --- Flexi-Time Flag ---
    is_flexi_checkin = fields.Boolean(
        string='Flexi Check-In',
        compute='_compute_is_late',
        store=True,
        help='Checked if employee used flexi-time (9:30-10:00 AM IST).')

    @api.depends('check_in')
    def _compute_is_late(self):
        """Determine if check-in is late (after 10:30 AM IST) or flexi (9:30-10:00 AM)."""
        for rec in self:
            if not rec.check_in:
                rec.is_late = False
                rec.is_flexi_checkin = False
                continue

            # Convert UTC to IST
            ist_checkin = rec.check_in + timedelta(hours=IST_HOURS, minutes=IST_MINUTES)
            checkin_time = ist_checkin.time()

            # Flexi: between 9:30 and 10:00 AM IST
            rec.is_flexi_checkin = FLEXI_START <= checkin_time < STANDARD_START

            # Late: after 10:30 AM IST (beyond grace period)
            rec.is_late = checkin_time > GRACE_END

    @api.depends('check_in', 'check_out')
    def _compute_ist_times(self):
        """Display check-in/out times in IST."""
        for rec in self:
            if rec.check_in:
                ist = rec.check_in + timedelta(hours=IST_HOURS, minutes=IST_MINUTES)
                rec.check_in_ist_time = ist.strftime('%I:%M %p')
            else:
                rec.check_in_ist_time = ''

            if rec.check_out:
                ist = rec.check_out + timedelta(hours=IST_HOURS, minutes=IST_MINUTES)
                rec.check_out_ist_time = ist.strftime('%I:%M %p')
            else:
                rec.check_out_ist_time = ''

    @api.depends('worked_hours')
    def _compute_productive_hours(self):
        """Productive hours = worked_hours - 1 hour break."""
        for rec in self:
            if rec.worked_hours:
                rec.productive_hours = max(0.0, rec.worked_hours - BREAK_HOURS)
                rec.is_productive_deficit = rec.productive_hours < MIN_PRODUCTIVE_HOURS
            else:
                rec.productive_hours = 0.0
                rec.is_productive_deficit = False
