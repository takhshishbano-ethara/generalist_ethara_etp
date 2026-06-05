import logging
from datetime import datetime, timedelta

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)

CATEGORY_SELECTION = [
    ('cab_taxi', 'Cab/Taxi'),
    ('meals', 'Meals'),
    ('travel', 'Travel'),
    ('other', 'Other'),
]

STATE_SELECTION = [
    ('draft', 'Draft'),
    ('submitted', 'Pending'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
    ('reimbursed', 'Reimbursed'),
]


class EtpReimbursement(models.Model):
    _name = 'etp.reimbursement'
    _description = 'Reimbursement Claim'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'submitted_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(
        string='Claim Reference',
        required=True, readonly=True, copy=False, default=lambda self: _('New'),
        tracking=True,
    )
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True, tracking=True,
        default=lambda self: self.env.user.employee_id.id,
        ondelete='restrict', index=True,
    )
    user_id = fields.Many2one(
        'res.users', string='Requested By',
        related='employee_id.user_id', store=True, index=True, readonly=True,
    )
    department_id = fields.Many2one(
        'hr.department', string='Department',
        related='employee_id.department_id', store=True, readonly=True,
    )
    request_date = fields.Date(
        string='Request Date', required=True, default=fields.Date.context_today,
        tracking=True, index=True,
    )
    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        default=lambda self: self.env.company.currency_id.id,
        required=True,
    )
    total_amount = fields.Monetary(
        string='Total', compute='_compute_total_amount',
        store=True, currency_field='currency_id', tracking=True,
    )
    line_ids = fields.One2many(
        'etp.reimbursement.line', 'reimbursement_id',
        string='Line Items', copy=True,
    )
    state = fields.Selection(
        STATE_SELECTION, string='Status', default='draft',
        required=True, tracking=True, index=True, copy=False,
    )
    submitted_date = fields.Datetime(string='Submitted On', readonly=True, copy=False)
    approved_date = fields.Datetime(string='Approved On', readonly=True, copy=False)
    rejected_date = fields.Datetime(string='Rejected On', readonly=True, copy=False)
    reimbursed_date = fields.Datetime(string='Reimbursed On', readonly=True, copy=False)
    approved_by = fields.Many2one('res.users', string='Approved By', readonly=True, copy=False)
    rejected_by = fields.Many2one('res.users', string='Rejected By', readonly=True, copy=False)
    reimbursed_by = fields.Many2one('res.users', string='Reimbursed By', readonly=True, copy=False)
    rejection_reason = fields.Text(string='Rejection Reason', tracking=True, copy=False)
    hr_comment = fields.Text(string='HR Comment', copy=False)
    line_count = fields.Integer(compute='_compute_line_count', store=False)

    @api.depends('line_ids', 'line_ids.amount')
    def _compute_total_amount(self):
        for rec in self:
            rec.total_amount = sum(rec.line_ids.mapped('amount'))

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.constrains('employee_id', 'request_date', 'state')
    def _check_one_request_per_day(self):
        for rec in self:
            if rec.state == 'draft' or not rec.employee_id:
                continue
            domain = [
                ('employee_id', '=', rec.employee_id.id),
                ('request_date', '=', rec.request_date),
                ('state', '!=', 'draft'),
                ('id', '!=', rec.id),
            ]
            if self.sudo().search_count(domain):
                raise ValidationError(_(
                    "%s already has a reimbursement request submitted on %s. "
                    "Only one request is allowed per day."
                ) % (rec.employee_id.name, rec.request_date))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'etp.reimbursement'
                ) or _('New')
        return super().create(vals_list)

    # ── State transitions ────────────────────────────────────────────────────

    def action_submit(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft claims can be submitted.'))
            if not rec.line_ids:
                raise UserError(_('Add at least one ride/expense line before submitting.'))
            rec.write({
                'state': 'submitted',
                'submitted_date': fields.Datetime.now(),
            })
            rec._notify_hr_new_request()
        return True

    def action_approve(self, comment=None):
        self._ensure_hr_user()
        for rec in self:
            if rec.state != 'submitted':
                raise UserError(_('Only pending claims can be approved.'))
            rec.write({
                'state': 'approved',
                'approved_date': fields.Datetime.now(),
                'approved_by': self.env.uid,
                'hr_comment': comment or rec.hr_comment,
            })
            rec._notify_requester_decision('approved')
        return True

    def action_reject(self, reason=None):
        self._ensure_hr_user()
        if not reason or not reason.strip():
            raise UserError(_('A rejection reason is required.'))
        for rec in self:
            if rec.state != 'submitted':
                raise UserError(_('Only pending claims can be rejected.'))
            rec.write({
                'state': 'rejected',
                'rejected_date': fields.Datetime.now(),
                'rejected_by': self.env.uid,
                'rejection_reason': reason.strip(),
            })
            rec._notify_requester_decision('rejected')
        return True

    def action_mark_reimbursed(self):
        self._ensure_hr_user()
        for rec in self:
            if rec.state != 'approved':
                raise UserError(_('Only approved claims can be marked as reimbursed.'))
            rec.write({
                'state': 'reimbursed',
                'reimbursed_date': fields.Datetime.now(),
                'reimbursed_by': self.env.uid,
            })
        return True

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _ensure_hr_user(self):
        if not self.env.user.has_group('etp_user_roles.group_hr_admin') and not self.env.user.has_group('base.group_system'):
            raise UserError(_('Only HR users can perform this action.'))

    def _hr_recipient_emails(self):
        hr_group = self.env.ref('etp_user_roles.group_hr_admin', raise_if_not_found=False)
        if not hr_group:
            return []
        emails = []
        for user in hr_group.sudo().user_ids:
            email = user.email or user.login
            if email:
                emails.append(email)
        return list(dict.fromkeys(emails))

    def _notify_hr_new_request(self):
        self.ensure_one()
        template = self.env.ref(
            'etp_reimbursement.email_template_reimbursement_new',
            raise_if_not_found=False,
        )
        recipients = self._hr_recipient_emails()
        if not template or not recipients:
            _logger.info('Skip HR notification: template=%s recipients=%s', bool(template), recipients)
            return
        try:
            template.sudo().with_context(hr_recipients=','.join(recipients)).send_mail(
                self.id, force_send=True, email_values={'email_to': ','.join(recipients)},
            )
        except Exception as e:
            _logger.error('Failed to notify HR about reimbursement %s: %s', self.name, e)

    def _notify_requester_decision(self, decision):
        self.ensure_one()
        xmlid = (
            'etp_reimbursement.email_template_reimbursement_approved'
            if decision == 'approved'
            else 'etp_reimbursement.email_template_reimbursement_rejected'
        )
        template = self.env.ref(xmlid, raise_if_not_found=False)
        recipient = self.user_id.email or self.user_id.login
        if not template or not recipient:
            _logger.info('Skip requester notification: template=%s email=%s', bool(template), recipient)
            return
        try:
            template.sudo().send_mail(
                self.id, force_send=True,
                email_values={'email_to': recipient},
            )
        except Exception as e:
            _logger.error('Failed to send %s notification for %s: %s', decision, self.name, e)


class EtpReimbursementLine(models.Model):
    _name = 'etp.reimbursement.line'
    _description = 'Reimbursement Line / Ride'
    _order = 'date desc, id desc'

    reimbursement_id = fields.Many2one(
        'etp.reimbursement', string='Claim',
        required=True, ondelete='cascade', index=True,
    )
    employee_id = fields.Many2one(
        related='reimbursement_id.employee_id', store=True, index=True,
    )
    state = fields.Selection(
        related='reimbursement_id.state', store=True, index=True,
    )
    date = fields.Date(string='Date', required=True, default=fields.Date.context_today)
    category = fields.Selection(
        CATEGORY_SELECTION, string='Category',
        required=True, default='cab_taxi',
    )
    description = fields.Char(string='Description', required=True)
    amount = fields.Monetary(string='Amount', required=True, currency_field='currency_id')
    currency_id = fields.Many2one(
        related='reimbursement_id.currency_id', store=True, readonly=True,
    )
    receipt_url = fields.Char(string='Receipt URL', help='S3 / CDN URL of the receipt photo.')

    @api.constrains('amount')
    def _check_amount(self):
        for rec in self:
            if rec.amount <= 0:
                raise ValidationError(_('Line amount must be greater than zero.'))
