from odoo import models, fields, api


class HrLeaveType(models.Model):
    _inherit = 'hr.leave.type'

    @api.model
    def _ethara_backfill_entitlements(self):
        """Backfill default entitlements on the original Ethara leave types.

        Called from data/leave_type_data.xml via <function> on every
        install/upgrade. The catalog records are noupdate="1", so plain data
        XML cannot update their values on systems where they already exist
        (stage/live); this writes them through the ORM instead. It only sets a
        value where it is still unset, so an HR/admin override is never lost.
        """
        defaults = {'sl': 12.0, 'cl': 12.0, 'el': 15.0}
        for code, days in defaults.items():
            leave_type = self.search([('ethara_leave_code', '=', code)], limit=1)
            if leave_type and not leave_type.default_annual_days:
                leave_type.default_annual_days = days

    # --- Ethara Policy Fields ---
    ethara_leave_code = fields.Selection([
        ('sl', 'Sick Leave'),
        ('cl', 'Casual Leave'),
        ('el', 'Earned Leave'),
        ('lop', 'Loss of Pay'),
        ('marriage', 'Marriage Leave'),
        ('maternity', 'Maternity Leave'),
        ('bereavement', 'Bereavement Leave'),
        ('comp_off', 'Comp-Off'),
        ('wfh', 'Work From Home'),
        ('menstrual', 'Menstrual Leave'),
        ('restricted_holiday', 'Restricted Holiday'),
    ], string='Ethara Leave Code',
        help='Internal code used by Ethara attendance/leave module for policy enforcement.')

    default_annual_days = fields.Float(
        string='Default Annual Days',
        default=0.0,
        help='Default yearly entitlement (days) used to pre-fill HR allocations for this leave '
             'type. Editable by HR/Admin; HR can override the count per employee when assigning.')

    allow_half_day = fields.Boolean(
        string='Allow Half Day',
        default=False,
        help='If checked, employees can request half-day leave for this type.')

    requires_medical_certificate = fields.Boolean(
        string='Requires Medical Certificate',
        default=False,
        help='If checked, medical certificate is required for leaves > 2 consecutive days.')

    medical_cert_threshold_days = fields.Integer(
        string='Medical Cert Required After (Days)',
        default=2,
        help='Number of consecutive days after which a medical certificate is required.')

    advance_notice_days = fields.Integer(
        string='Advance Notice (Days)',
        default=0,
        help='Minimum days in advance the leave must be requested. 0 = no restriction.')

    lapses_at_year_end = fields.Boolean(
        string='Lapses at Year End',
        default=True,
        help='If checked, unused balance of this leave type expires on Dec 31.')

    max_carry_forward_days = fields.Float(
        string='Max Carry Forward (Days)',
        default=0,
        help='Maximum days that can be carried forward to the next year. 0 = no carry forward.')

    apply_sandwich_rule = fields.Boolean(
        string='Apply Sandwich Rule',
        default=False,
        help='If checked, weekends between two leave days are counted as leave (e.g., Fri+Mon = 4 days).')

    monthly_accrual_days = fields.Float(
        string='Monthly Accrual (Days)',
        default=1.0,
        help='Number of days accrued per month for this leave type.')
