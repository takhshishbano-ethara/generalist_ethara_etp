import logging
from datetime import timedelta

from odoo import models, fields, api
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    # --- Generic attachment (any leave type) ---
    # Used by the TaskForge API to let employees attach supporting evidence
    # (medical note, travel docs, etc.) at apply time, viewable by approvers.
    #
    # Storage strategy: new uploads land in S3 via fenrir.s3.service and the
    # object key is recorded in `attachment_s3_key`. The legacy Binary
    # column is kept for backwards compatibility with any leaves that were
    # created before the switch — readers fall back to it when the S3 key
    # is empty. New writes never touch the Binary column.
    attachment = fields.Binary(
        string='Attachment (legacy local)',
        attachment=True,
        help='Legacy: base64 / ir.attachment storage for files uploaded '
             'before S3 offload landed. Read-only for new code.')

    attachment_filename = fields.Char(
        string='Attachment Filename')

    attachment_s3_key = fields.Char(
        string='Attachment S3 Key',
        help='Object key under the configured S3 bucket. Set by the '
             'TaskForge API when a new attachment is uploaded.')

    # --- Medical Certificate ---
    medical_certificate = fields.Binary(
        string='Medical Certificate',
        attachment=True,
        help='Required for Sick Leave exceeding 2 consecutive days.')

    medical_certificate_filename = fields.Char(
        string='Certificate Filename')

    medical_cert_required = fields.Boolean(
        string='Medical Cert Required',
        compute='_compute_medical_cert_required',
        store=True,
        help='Auto-set when SL exceeds threshold days.')

    medical_cert_deadline = fields.Date(
        string='Medical Cert Deadline',
        compute='_compute_medical_cert_required',
        store=True,
        help='Deadline to upload medical certificate (2 days after return).')

    # --- Sandwich Rule Adjusted Days ---
    sandwich_days_added = fields.Float(
        string='Sandwich Days Added',
        default=0,
        help='Additional weekend days added due to the sandwich rule.')

    # --- TaskForge API decision audit ---
    # Populated by the approve/reject endpoints in task_forge_bridge so the
    # API can answer "who decided, when, with what comment" uniformly on
    # both the approval and the rejection paths. Standard Odoo only tracks
    # the approver (first_approver_id / second_approver_id) — there is no
    # built-in field for the refuser or the decision text.
    x_decision_user_id = fields.Many2one(
        'res.users',
        string='Decision User',
        help='User who took the final decision (approve or reject) via the TaskForge API.')

    x_decision_date = fields.Datetime(
        string='Decision Date',
        help='When the TaskForge API approve/reject endpoint was called.')

    x_decision_comment = fields.Text(
        string='Decision Comment',
        help='Optional comment the approver/rejector typed alongside the action.')

    def _get_leaves_on_public_holiday(self):
        """Skip 'not supposed to work' error for employees missing a resource calendar."""
        problem_leaves = super()._get_leaves_on_public_holiday()
        return problem_leaves.filtered(
            lambda l: l.employee_id.resource_calendar_id
        )

    @api.depends('holiday_status_id', 'number_of_days', 'request_date_from', 'request_date_to')
    def _compute_medical_cert_required(self):
        """Check if medical certificate is required for SL > threshold days."""
        for leave in self:
            leave_type = leave.holiday_status_id
            if (leave_type and
                    leave_type.ethara_leave_code == 'sl' and
                    leave_type.requires_medical_certificate and
                    leave.number_of_days > leave_type.medical_cert_threshold_days):
                leave.medical_cert_required = True
                # Deadline: 2 working days after return (request_date_to + 2 days)
                if leave.request_date_to:
                    leave.medical_cert_deadline = leave.request_date_to + timedelta(days=2)
                else:
                    leave.medical_cert_deadline = False
            else:
                leave.medical_cert_required = False
                leave.medical_cert_deadline = False

    @api.constrains('holiday_status_id', 'request_date_from', 'number_of_days')
    def _check_el_advance_notice(self):
        """EL requires 20 days advance notice."""
        for leave in self:
            leave_type = leave.holiday_status_id
            if not leave_type or leave_type.ethara_leave_code != 'el':
                continue

            if leave_type.advance_notice_days and leave.request_date_from:
                days_until = (leave.request_date_from - fields.Date.today()).days
                if days_until < leave_type.advance_notice_days:
                    raise ValidationError(
                        'Earned Leave requires at least %d days advance notice. '
                        'You are requesting leave starting %s (%d days from today).' % (
                            leave_type.advance_notice_days,
                            leave.request_date_from,
                            days_until)
                    )

    @api.constrains('holiday_status_id', 'request_unit_half')
    def _check_half_day_restriction(self):
        """Only CL allows half-day. SL and EL are full-day only."""
        for leave in self:
            leave_type = leave.holiday_status_id
            if not leave_type:
                continue

            # Check if it's a half-day request
            if leave.request_unit_half:
                if leave_type.ethara_leave_code in ('sl', 'el'):
                    raise ValidationError(
                        '%s is full-day only. Half-day requests are not allowed.' % leave_type.name
                    )

    def action_approve(self, check_state=True):
        """
        Override to apply the sandwich rule for CL:
        If CL is taken on a Friday and the following Monday, include Sat+Sun.
        """
        for leave in self:
            leave_type = leave.holiday_status_id
            if leave_type and leave_type.apply_sandwich_rule and leave_type.ethara_leave_code == 'cl':
                leave._apply_sandwich_rule()

        return super().action_approve(check_state=check_state)

    def _apply_sandwich_rule(self):
        """
        Sandwich Rule: If CL spans across a weekend (e.g., Friday + Monday),
        the intervening Saturday and Sunday count as leave days.
        """
        self.ensure_one()
        if not self.request_date_from or not self.request_date_to:
            return

        date_from = self.request_date_from
        date_to = self.request_date_to

        sandwich_days = 0
        current = date_from
        while current <= date_to:
            # Check if current day is a Friday
            if current.weekday() == 4:  # Friday
                saturday = current + timedelta(days=1)
                sunday = current + timedelta(days=2)
                monday = current + timedelta(days=3)

                # If Monday is also within the leave range or has a separate leave,
                # then Saturday and Sunday are sandwiched
                if monday <= date_to:
                    sandwich_days += 2
                else:
                    # Check if there's a separate leave on Monday for same employee
                    monday_leave = self.sudo().search([
                        ('employee_id', '=', self.employee_id.id),
                        ('request_date_from', '<=', monday),
                        ('request_date_to', '>=', monday),
                        ('state', 'not in', ['refuse']),
                        ('id', '!=', self.id),
                    ], limit=1)
                    if monday_leave:
                        sandwich_days += 2

            current += timedelta(days=1)

        if sandwich_days > 0:
            self.write({
                'sandwich_days_added': sandwich_days,
                'number_of_days': self.number_of_days + sandwich_days,
            })
            self.message_post(
                body='Sandwich Rule applied: %d weekend day(s) added to leave count. '
                     'Total days: %s' % (sandwich_days, self.number_of_days),
                subtype_xmlid='mail.mt_note',
            )

    # --- Medical Certificate Deadline Check ---
    @api.model
    def _cron_check_medical_certificates(self):
        """
        Daily cron: If SL > 2 days and no medical certificate uploaded
        within 2 days of return, notify HR to mark as LWP.
        """
        today = fields.Date.today()
        overdue_leaves = self.sudo().search([
            ('medical_cert_required', '=', True),
            ('medical_certificate', '=', False),
            ('medical_cert_deadline', '<', today),
            ('state', '=', 'validate'),
        ])

        for leave in overdue_leaves:
            try:
                # Notify HR
                hr_group = self.env.ref('hr.group_hr_manager', raise_if_not_found=False)
                if hr_group:
                    for hr_user in hr_group.users:
                        leave.message_post(
                            body='ALERT: Medical certificate not uploaded for %s\'s Sick Leave '
                                 '(%s to %s, %s days). Deadline was %s. '
                                 'Please review and consider marking as Leave Without Pay (LWP).' % (
                                     leave.employee_id.name,
                                     leave.request_date_from,
                                     leave.request_date_to,
                                     leave.number_of_days,
                                     leave.medical_cert_deadline,
                                 ),
                            partner_ids=hr_user.partner_id.ids,
                            subtype_xmlid='mail.mt_note',
                        )
                _logger.warning(
                    'Medical cert overdue: %s - leave %s',
                    leave.employee_id.name, leave.id
                )
            except Exception as e:
                _logger.error('Failed to send medical cert alert for leave %s: %s', leave.id, str(e))
