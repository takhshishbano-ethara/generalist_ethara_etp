import logging

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)

CATEGORY_SELECTION = [
    ('cab_taxi', 'Cab/Taxi'),
    ('meals', 'Meals'),
    ('travel', 'Travel'),
    ('other', 'Other'),
]

STATE_SELECTION = [
    ('draft', 'Draft'),
    ('submitted', 'Manager Review'),
    ('hr_review', 'HR Review'),
    ('approved', 'Finance Payout'),
    ('rejected', 'Rejected'),
    ('reimbursed', 'Reimbursed'),
]

# The fixed 5-stage approval flow shown in the Reimbursement module and the
# Wiki → Process Flow page (single source of truth). `state_at` is the claim
# state at which this stage is the CURRENT one.
REIMBURSEMENT_FLOW = [
    {'sequence': 1, 'key': 'submit', 'state_at': 'draft',
     'title': 'Submit claim', 'kind': 'stage', 'role': 'YOU',
     'owner': 'You', 'duration': 'Anytime — within 30 days of the expense',
     'description': 'You file the expense with itemised receipts.'},
    {'sequence': 2, 'key': 'manager', 'state_at': 'submitted',
     'title': 'Manager review', 'kind': 'review', 'role': 'MANAGER',
     'owner': 'Manager', 'duration': 'Typically 1–2 working days',
     'description': 'Your reporting manager checks the claim against policy.'},
    {'sequence': 3, 'key': 'hr', 'state_at': 'hr_review',
     'title': 'HR review', 'kind': 'review', 'role': 'PEOPLE OPS',
     'owner': 'People Ops',
     'duration': 'Typically 2–3 working days',
     'description': 'People Ops verifies receipts and category eligibility.'},
    {'sequence': 4, 'key': 'finance', 'state_at': 'approved',
     'title': 'Finance payout', 'kind': 'stage', 'role': 'FINANCE',
     'owner': 'Finance', 'duration': 'Batched into the next payout run',
     'description': 'Finance authorises the approved amount for payment.'},
    {'sequence': 5, 'key': 'reimbursed', 'state_at': 'reimbursed',
     'title': 'Reimbursed', 'kind': 'outcome', 'role': 'FINANCE',
     'owner': 'Finance', 'duration': 'Credited with the next salary cycle',
     'description': 'The amount is credited to your salary account.'},
]

# Claim state -> sequence number of the stage that is currently active.
STATE_TO_STAGE = {
    'draft': 1, 'submitted': 2, 'hr_review': 3,
    'approved': 4, 'reimbursed': 5, 'rejected': 0,
}


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
        default=lambda self: (
            self.env.ref('base.INR', raise_if_not_found=False)
            or self.env.company.currency_id
        ).id,
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
    manager_approved_date = fields.Datetime(
        string='Manager Reviewed On', readonly=True, copy=False)
    manager_approved_by = fields.Many2one(
        'res.users', string='Manager Reviewed By', readonly=True, copy=False)
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
            # HR users are exempt from the one-per-day limit
            if self._user_is_hr(self.env.user):
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

    @api.model
    def _user_is_hr(self, user):
        role_name = (user.user_role.name or '').strip().lower() if user.user_role else ''
        return role_name in {'hr', 'hr admin'} \
            or user.has_group('etp_user_roles.group_hr_admin') \
            or user.has_group('etp_reimbursement.group_reimbursement_manager') \
            or user.has_group('base.group_system')

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
        """Advance one review stage. A claim must clear BOTH the manager
        review and the HR review before it reaches Finance Payout — so this
        moves submitted → hr_review on the first approval and
        hr_review → approved on the second (the decision gate)."""
        self._ensure_hr_user()
        now = fields.Datetime.now()
        for rec in self:
            if rec.state == 'submitted':
                rec.write({
                    'state': 'hr_review',
                    'manager_approved_date': now,
                    'manager_approved_by': self.env.uid,
                    'hr_comment': comment or rec.hr_comment,
                })
            elif rec.state == 'hr_review':
                rec.write({
                    'state': 'approved',
                    'approved_date': now,
                    'approved_by': self.env.uid,
                    'hr_comment': comment or rec.hr_comment,
                })
                rec._notify_requester_decision('approved')
            else:
                raise UserError(_('This claim is not awaiting a review.'))
        return True

    def action_reject(self, reason=None):
        self._ensure_hr_user()
        if not reason or not reason.strip():
            raise UserError(_('A rejection reason is required.'))
        for rec in self:
            if rec.state not in ('submitted', 'hr_review'):
                raise UserError(_('Only claims under review can be rejected.'))
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

    # ── 5-stage approval flow (single source of truth) ───────────────────────

    @api.model
    def get_flow_meta(self):
        """Static documentation for the reimbursement approval flow. The
        Wiki → Process Flow page renders this so there is one source of truth
        for the process."""
        return {
            'title': 'Reimbursement approval flow',
            'subtitle': 'Operations process documentation — how an expense '
                        'claim moves from submission to payout.',
            'owner': 'People Ops',
            'version': 'v3',
            'total_stages': len(REIMBURSEMENT_FLOW),
            'decision_gate': {
                'title': 'Decision gate · steps 2–3',
                'text': 'A claim advances to payout only after both the '
                        'manager and People Ops approve it. If either rejects '
                        'it, you will see the reason on the claim and can edit '
                        'and resubmit.',
            },
            'legend': [
                {'key': 'stage', 'label': 'Stage',
                 'description': 'A step you or another team completes.'},
                {'key': 'outcome', 'label': 'Outcome',
                 'description': 'The claim is approved and paid out.'},
            ],
            'stages': [dict(stage) for stage in REIMBURSEMENT_FLOW],
        }

    def _stage_timestamp(self, key):
        self.ensure_one()
        return {
            'submit': self.submitted_date,
            'manager': self.manager_approved_date,
            'hr': self.approved_date,
            'finance': self.approved_date,
            'reimbursed': self.reimbursed_date,
        }.get(key)

    def flow_stages(self):
        """This claim's five stages, each tagged completed / current /
        pending, with the timestamp captured at that stage."""
        self.ensure_one()
        current = STATE_TO_STAGE.get(self.state, 0)
        rejected = self.state == 'rejected'
        result = []
        for stage in REIMBURSEMENT_FLOW:
            seq = stage['sequence']
            if rejected:
                done = (stage['key'] == 'submit' and self.submitted_date) or \
                       (stage['key'] == 'manager' and self.manager_approved_date)
                status = 'completed' if done else 'pending'
            elif seq < current:
                status = 'completed'
            elif seq == current:
                status = 'current'
            else:
                status = 'pending'
            ts = self._stage_timestamp(stage['key'])
            result.append(dict(
                stage,
                status=status,
                is_current=(not rejected and seq == current),
                timestamp=ts.isoformat() if ts else False,
            ))
        return result

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _ensure_hr_user(self):
        user = self.env.user
        role_name = (user.user_role.name or '').strip().lower() if user.user_role else ''
        if role_name in {'hr', 'hr admin'}:
            return
        if user.has_group('etp_user_roles.group_hr_admin') \
                or user.has_group('etp_reimbursement.group_reimbursement_manager') \
                or user.has_group('base.group_system'):
            return
        raise UserError(_('Only HR users can perform this action.'))

    def _hr_recipient_emails(self):
        Users = self.env['res.users'].sudo()
        users = Users.browse()
        hr_group = self.env.ref('etp_user_roles.group_hr_admin', raise_if_not_found=False)
        if hr_group:
            users |= hr_group.sudo().user_ids
        users |= Users.search([('user_role.name', 'in', ['HR', 'hr', 'HR Admin', 'hr admin'])])
        emails = []
        for user in users:
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
