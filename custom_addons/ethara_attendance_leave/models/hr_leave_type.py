from odoo import models, fields


class HrLeaveType(models.Model):
    _inherit = 'hr.leave.type'

    # --- Ethara Policy Fields ---
    ethara_leave_code = fields.Selection([
        ('sl', 'Sick Leave'),
        ('cl', 'Casual Leave'),
        ('el', 'Earned Leave'),
    ], string='Ethara Leave Code',
        help='Internal code used by Ethara attendance/leave module for policy enforcement.')

    available_during_probation = fields.Boolean(
        string='Available During Probation',
        default=False,
        help='If checked, this leave type can be taken by employees in probation.')

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
